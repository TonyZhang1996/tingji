package com.tingji.app.engine

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import kotlin.math.PI
import kotlin.math.sin
import kotlin.math.sqrt

class EnginePureTests {
    private fun wav(file:File,seconds:Int,rate:Int=16000,channels:Int=1) {
        val bytes=seconds.toLong()*rate*channels*2
        RandomAccessFile(file,"rw").use {f->
            val b=ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
            b.put("RIFF".toByteArray()).putInt((bytes+36).toInt()).put("WAVEfmt ".toByteArray()).putInt(16)
            b.putShort(1).putShort(channels.toShort()).putInt(rate).putInt(rate*channels*2).putShort((channels*2).toShort()).putShort(16)
            b.put("data".toByteArray()).putInt(bytes.toInt());f.write(b.array());f.setLength(bytes+44)
        }
    }
    @Test fun longWavSeekAndBoundedSlicePreserveDuration() {
        val dir=Files.createTempDirectory("tingji-wave-test").toFile()
        try {
            val source=File(dir,"long.wav");wav(source,7200)
            assertEquals(7200.0,AudioTools.wavInfo(source).duration,.0001)
            val piece=File(dir,"piece.wav");AudioTools.slice(source,piece,3600.25,3710.25){false}
            assertEquals(110.0,AudioTools.wavInfo(piece).duration,.0001)
            assertEquals(44+110*32000,piece.length().toInt())
            try {AudioTools.slice(source,piece,0.0,120.0){false};fail("API boundary must be enforced") } catch(_:IllegalArgumentException){}
        } finally {dir.deleteRecursively()}
    }
    @Test fun normalizationMixesChannelsWithoutWholeAudioMemory() {
        val dir=Files.createTempDirectory("tingji-normalize-test").toFile()
        try {
            val source=File(dir,"stereo.wav");wav(source,1,48000,2)
            RandomAccessFile(source,"rw").use {f-> f.seek(44);repeat(48000){f.writeShort(java.lang.Short.reverseBytes(12000).toInt());f.writeShort(java.lang.Short.reverseBytes(-12000).toInt())} }
            val output=File(dir,"normalized.wav");AudioTools.normalize(source,output,{false}){}
            val info=AudioTools.wavInfo(output)
            assertEquals(16000,info.sampleRate);assertEquals(1,info.channels);assertEquals(1.0,info.duration,.001)
            val data=output.readBytes().drop(44);assertTrue(data.all{it.toInt()==0})
        } finally {dir.deleteRecursively()}
    }
    @Test fun cancelledConversionNeverLeavesUsablePartialAudio() {
        val dir=Files.createTempDirectory("tingji-cancel-test").toFile()
        try {
            val source=File(dir,"source.wav");wav(source,5)
            val output=File(dir,"normalized.wav")
            try {AudioTools.normalize(source,output,{true}){};fail("must cancel")}catch(_:TaskCancelled){}
            assertFalse(output.exists());assertFalse(File(dir,"normalized.wav.part").exists());assertTrue(source.isFile)
        } finally {dir.deleteRecursively()}
    }
    @Test fun downsamplingRejectsHighFrequencyAliasingButKeepsSpeechBand() {
        val dir=Files.createTempDirectory("tingji-resample-test").toFile()
        try {
            fun normalizedRms(frequency:Int):Double {
                val source=File(dir,"$frequency.wav");wav(source,1,48000)
                RandomAccessFile(source,"rw").use{f->f.seek(44);repeat(48000){i->f.writeShort(java.lang.Short.reverseBytes((sin(2*PI*frequency*i/48000)*16000).toInt().toShort()).toInt())}}
                val result=File(dir,"$frequency-normalized.wav");AudioTools.normalize(source,result,{false}){}
                val info=AudioTools.wavInfo(result);assertEquals(1.0,info.duration,.001)
                val buffer=ByteBuffer.wrap(result.readBytes(),44,(info.frames*2).toInt()).order(ByteOrder.LITTLE_ENDIAN)
                val values=DoubleArray(info.frames.toInt()){buffer.short/32768.0}.drop(100).dropLast(100)
                return sqrt(values.sumOf{it*it}/values.size)
            }
            assertTrue(normalizedRms(1000)>.30)
            assertTrue(normalizedRms(12000)<.02)
        } finally {dir.deleteRecursively()}
    }
    @Test fun completeFileFingerprintDetectsMiddleChanges() {
        val file=Files.createTempFile("tingji-hash-test",".bin").toFile()
        try {file.writeBytes(ByteArray(200000));val first=fileHash(file);RandomAccessFile(file,"rw").use{it.seek(100000);it.write(1)};assertNotEquals(first,fileHash(file))}finally{file.delete()}
    }
    private fun wire():JSONObject = JSONObject().put("overview","两人讨论发布安排").put("topics",JSONArray()).put("decisions",JSONArray()).put("open_questions",JSONArray())
        .put("todos",JSONArray().put(JSONObject().put("task","完成测试").put("owner","甲").put("deadline","明天").put("evidence","00:00:01")))
        .put("speaker_summaries",JSONObject().put("S1",person("甲承诺测试")).put("S2",person("乙确认验收")))
    private fun person(text:String)=JSONObject().put("summary",text).put("points",JSONArray()).put("commitments",JSONArray()).put("evidence","00:00:01")
    @Test fun summaryRequiresEverySpeakerAndSuppliesNamesLocally() {
        val roster=linkedMapOf("S1" to "甲","S2" to "乙")
        val output=DeepSeekClient.validateSummary(wire(),roster)
        assertEquals("乙",output.getJSONArray("speaker_summaries").getJSONObject(1).getString("speaker_name"))
        val bad=wire();bad.getJSONObject("speaker_summaries").remove("S2")
        try{DeepSeekClient.validateSummary(bad,roster);fail("missing speaker must fail")}catch(_:IllegalArgumentException){}
    }
    @Test fun malformedSummaryCannotBeAcceptedAsExistingReport() {
        val bad=wire().put("unexpected",true)
        try{DeepSeekClient.validateSummary(bad,linkedMapOf("S1" to "甲","S2" to "乙"));fail("extra fields must fail")}catch(_:IllegalArgumentException){}
    }
    @Test fun exportAlwaysEndsWithTodosAndKeepsBothPeople() {
        val report=JSONObject().put("coverage",JSONObject().put("full",true)).put("content",DeepSeekClient.validateSummary(wire(),linkedMapOf("S1" to "甲","S2" to "乙")))
        val markdown=MeetingSummary.markdown("测试会议",report)
        val headings=Regex("(?m)^## (.+)$").findAll(markdown).map{it.groupValues[1]}.toList()
        assertEquals("待办事项",headings.last());assertTrue(markdown.contains("甲（S1）"));assertTrue(markdown.contains("乙（S2）"))
    }
}
