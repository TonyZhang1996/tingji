package com.tingji.app.engine

import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/** All decoding is streamed; only a codec buffer and a small PCM block are held. */
object AudioTools {
    const val RATE = 16000
    data class WavInfo(val frames: Long, val offset: Long, val sampleRate: Int, val channels: Int, val bits: Int) {
        val duration: Double get() = frames.toDouble() / sampleRate
    }

    fun duration(file: File): Double {
        try { return wavInfo(file).duration } catch (_: Exception) { }
        val reader = MediaMetadataRetriever()
        try {
            reader.setDataSource(file.absolutePath)
            val millis = reader.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toDoubleOrNull() ?: 0.0
            require(millis > 0) { "无法读取录音时长，请确认文件能正常播放。" }
            return millis / 1000.0
        } finally { reader.release() }
    }

    fun wavInfo(file: File): WavInfo = RandomAccessFile(file,"r").use { f ->
        require(f.length() >= 44 && readFour(f)=="RIFF") { "不是 PCM WAV" }
        f.skipBytes(4); require(readFour(f)=="WAVE")
        var format=0; var channels=0; var rate=0; var bits=0
        while (f.filePointer+8<=f.length()) {
            val tag=readFour(f); val size=readInt(f).toLong() and 0xffffffffL; val next=f.filePointer+size+(size and 1L)
            require(next<=f.length()+1) { "录音文件不完整。" }
            if(tag=="fmt ") {
                require(size>=16); format=readShort(f); channels=readShort(f); rate=readInt(f)
                f.skipBytes(6); bits=readShort(f)
            } else if(tag=="data") {
                require(format==1 && channels in 1..8 && rate in 8000..192000 && bits==16) { "需要16位 PCM WAV。" }
                require(size%(channels*2)==0L)
                return@use WavInfo(size/(channels*2),f.filePointer,rate,channels,bits)
            }
            f.seek(next)
        }
        throw IllegalArgumentException("WAV 没有音频内容。")
    }

    fun normalize(source: File, target: File, cancelled:()->Boolean, progress:(String)->Unit) {
        if(target.isFile) {
            try { val info=wavInfo(target); if(info.sampleRate==RATE && info.channels==1 && info.frames>0) return } catch (_:Exception) { }
        }
        target.parentFile?.mkdirs()
        val partial=File(target.parentFile,target.name+".part")
        val output=RandomAccessFile(partial,"rw")
        try {
            output.setLength(0); output.write(ByteArray(44))
            val pcm = PcmWriter(output)
            val wav = try { wavInfo(source) } catch (_:Exception) { null }
            if(wav!=null) {
                RandomAccessFile(source,"r").use { input ->
                    input.seek(wav.offset)
                    var remaining=wav.frames*wav.channels*2
                    val block=ByteArray(64*1024 - (64*1024)%(wav.channels*2))
                    while(remaining>0) {
                        checkCancel(cancelled)
                        val n=input.read(block,0,min(block.size.toLong(),remaining).toInt())
                        require(n>0) { "录音文件不完整。" }
                        pcm.accept(ByteBuffer.wrap(block,0,n).order(ByteOrder.LITTLE_ENDIAN),wav.sampleRate,wav.channels,AudioFormat.ENCODING_PCM_16BIT)
                        remaining-=n
                    }
                }
            } else decode(source,pcm,cancelled,progress)
            pcm.finish()
            require(output.length()>44) { "没有解码出音频。" }
            writeHeader(output,output.length()-44)
            output.fd.sync(); output.close()
            checkCancel(cancelled)
            require(partial.renameTo(target)) { "无法保存转换后的音频。" }
        } catch(e:Exception) { try { output.close() } catch(_:Exception) {}; partial.delete(); throw e }
    }

    private fun decode(source:File, pcm:PcmWriter, cancelled:()->Boolean, progress:(String)->Unit) {
        val extractor=MediaExtractor(); var codec:MediaCodec?=null
        try {
            extractor.setDataSource(source.absolutePath)
            val track=(0 until extractor.trackCount).firstOrNull { extractor.getTrackFormat(it).getString(MediaFormat.KEY_MIME)?.startsWith("audio/")==true }
                ?: throw IllegalArgumentException("文件中没有可读取的音轨。")
            extractor.selectTrack(track)
            val format=extractor.getTrackFormat(track); val mime=format.getString(MediaFormat.KEY_MIME)!!
            if(mime=="audio/raw") {
                val buffer=ByteBuffer.allocate(256*1024).order(ByteOrder.LITTLE_ENDIAN)
                while(true) {
                    checkCancel(cancelled); buffer.clear()
                    val n=extractor.readSampleData(buffer,0); if(n<0) break
                    buffer.position(0); buffer.limit(n)
                    val encoding=if(format.containsKey(MediaFormat.KEY_PCM_ENCODING))format.getInteger(MediaFormat.KEY_PCM_ENCODING) else AudioFormat.ENCODING_PCM_16BIT
                    pcm.accept(buffer,format.getInteger(MediaFormat.KEY_SAMPLE_RATE),format.getInteger(MediaFormat.KEY_CHANNEL_COUNT),encoding)
                    extractor.advance()
                }
                return
            }
            codec=MediaCodec.createDecoderByType(mime); codec.configure(format,null,null,0); codec.start()
            var inputEnd=false; var outputEnd=false; var outputFormat=format
            val info=MediaCodec.BufferInfo(); var lastNotice=0L; var lastActivity=System.nanoTime()
            while(!outputEnd) {
                checkCancel(cancelled)
                if(!inputEnd) {
                    val index=codec.dequeueInputBuffer(10000)
                    if(index>=0) {
                        val buffer=codec.getInputBuffer(index)!!; buffer.clear()
                        val n=extractor.readSampleData(buffer,0)
                        if(n<0) { codec.queueInputBuffer(index,0,0,0,MediaCodec.BUFFER_FLAG_END_OF_STREAM); inputEnd=true }
                        else { codec.queueInputBuffer(index,0,n,extractor.sampleTime,0); extractor.advance() }
                        lastActivity=System.nanoTime()
                    }
                }
                val index=codec.dequeueOutputBuffer(info,10000)
                if(index==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) outputFormat=codec.outputFormat
                else if(index>=0) {
                    try {
                        if(info.size>0) {
                            val b=codec.getOutputBuffer(index)!!.duplicate().order(ByteOrder.LITTLE_ENDIAN)
                            b.position(info.offset); b.limit(info.offset+info.size)
                            val encoding=if(outputFormat.containsKey(MediaFormat.KEY_PCM_ENCODING)) outputFormat.getInteger(MediaFormat.KEY_PCM_ENCODING) else AudioFormat.ENCODING_PCM_16BIT
                            pcm.accept(b,outputFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE),outputFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT),encoding)
                            lastActivity=System.nanoTime()
                        }
                        outputEnd=info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                        if(info.presentationTimeUs-lastNotice>15_000_000) { progress("正在转换音频：${clock(info.presentationTimeUs/1_000_000.0)}"); lastNotice=info.presentationTimeUs }
                    } finally { codec.releaseOutputBuffer(index,false) }
                }
                require(System.nanoTime()-lastActivity<60_000_000_000L) { "音频解码长时间没有进展，请尝试导出为 MP3、M4A 或 WAV。" }
            }
        } finally { try { codec?.stop() } catch(_:Exception) {}; codec?.release(); extractor.release() }
    }

    private class PcmWriter(private val output:RandomAccessFile) {
        private var configuredRate=0; private var sourceIndex=-1L; private var nextPosition=0.0
        private val ring=FloatArray(64)
        private var coefficients:Array<FloatArray>?=null
        private val bytes=ByteArray(32768); private var used=0
        fun accept(buffer:ByteBuffer,rate:Int,channels:Int,encoding:Int) {
            require(rate in 8000..192000 && channels in 1..8)
            require(encoding==AudioFormat.ENCODING_PCM_16BIT || encoding==AudioFormat.ENCODING_PCM_FLOAT) { "暂不支持此音频采样格式，请转为16位 WAV。" }
            if(configuredRate==0) {
                configuredRate=rate
                if(rate!=RATE) {
                    // Band-limited polyphase resampling. Merely dropping 48k
                    // samples would alias high frequencies into speech bands.
                    val cutoff=.47*min(1.0,RATE.toDouble()/rate)
                    coefficients=Array(256) {phase->
                        val fraction=phase/256.0
                        val values=DoubleArray(32) {tap->
                            val distance=tap-15-fraction
                            val sinc=if(abs(distance)<1e-12)2*cutoff else sin(2*PI*cutoff*distance)/(PI*distance)
                            sinc*(.5+.5*cos(PI*distance/16))
                        }
                        val sum=values.sum()
                        FloatArray(32){(values[it]/sum).toFloat()}
                    }
                }
            }
            require(rate==configuredRate) { "录音采样率在中途改变。" }
            val size=if(encoding==AudioFormat.ENCODING_PCM_FLOAT)4 else 2
            require(buffer.remaining()%(channels*size)==0)
            while(buffer.remaining()>=channels*size) {
                var value=0f
                repeat(channels) { value+=if(encoding==AudioFormat.ENCODING_PCM_FLOAT)buffer.float else buffer.short/32768f }
                value=(value/channels).coerceIn(-1f,1f); sourceIndex++
                if(rate==RATE)writeSample(value)
                else {ring[(sourceIndex and 63L).toInt()]=value;emitAvailable(false)}
            }
        }
        private fun emitAvailable(final:Boolean) {
            val bank=coefficients?:return
            while(nextPosition<sourceIndex+1 && (final || floor(nextPosition)+16<=sourceIndex)) {
                val center=floor(nextPosition).toLong()
                val phase=((nextPosition-center)*256).toInt().coerceIn(0,255)
                var value=0f
                for(tap in 0 until 32) {
                    val index=center+tap-15
                    if(index in 0..sourceIndex)value+=ring[(index and 63L).toInt()]*bank[phase][tap]
                }
                writeSample(value);nextPosition+=configuredRate.toDouble()/RATE
            }
        }
        private fun writeSample(value:Float) {
            val short=(value.coerceIn(-1f,1f)*32768).roundToInt().coerceIn(-32768,32767);bytes[used++]=short.toByte();bytes[used++]=(short shr 8).toByte()
            if(used==bytes.size)flush()
        }
        private fun flush() { output.write(bytes,0,used); used=0; require(output.length()<0xfffffff0L) { "转换后的音频超过 WAV 容量限制。" } }
        fun finish() {if(configuredRate!=RATE)emitAvailable(true);flush()}
    }

    fun slice(source:File,target:File,start:Double,end:Double,cancelled:()->Boolean) {
        val info=wavInfo(source); require(info.sampleRate==RATE && info.channels==1)
        require(start>=0 && end>start && end-start<=110.01)
        val first=(start*RATE).toLong().coerceAtMost(info.frames)
        val last=(end*RATE).toLong().coerceAtMost(info.frames)
        require(last>first)
        RandomAccessFile(source,"r").use { input -> RandomAccessFile(target,"rw").use { output ->
            output.setLength(0); writeHeader(output,(last-first)*2); input.seek(info.offset+first*2)
            var remaining=(last-first)*2; val block=ByteArray(65536)
            while(remaining>0) { checkCancel(cancelled); val n=min(block.size.toLong(),remaining).toInt(); input.readFully(block,0,n); output.write(block,0,n); remaining-=n }
        } }
    }
    private fun checkCancel(cancelled:()->Boolean) { if(cancelled()) throw TaskCancelled() }
    private fun readFour(f:RandomAccessFile)=ByteArray(4).also { f.readFully(it) }.toString(Charsets.US_ASCII)
    private fun readInt(f:RandomAccessFile)=Integer.reverseBytes(f.readInt())
    private fun readShort(f:RandomAccessFile)=java.lang.Short.reverseBytes(f.readShort()).toInt() and 65535
    private fun writeHeader(f:RandomAccessFile,bytes:Long) {
        require(bytes in 0..0xffffffd0L); f.seek(0)
        f.writeBytes("RIFF"); f.writeInt(Integer.reverseBytes((bytes+36).toInt())); f.writeBytes("WAVEfmt ")
        f.writeInt(Integer.reverseBytes(16)); f.writeShort(java.lang.Short.reverseBytes(1).toInt()); f.writeShort(java.lang.Short.reverseBytes(1).toInt())
        f.writeInt(Integer.reverseBytes(RATE)); f.writeInt(Integer.reverseBytes(RATE*2)); f.writeShort(java.lang.Short.reverseBytes(2).toInt()); f.writeShort(java.lang.Short.reverseBytes(16).toInt())
        f.writeBytes("data"); f.writeInt(Integer.reverseBytes(bytes.toInt()))
    }
}
