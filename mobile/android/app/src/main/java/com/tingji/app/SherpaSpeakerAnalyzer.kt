package com.tingji.app

import android.content.Context
import android.util.AtomicFile
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.SpeakerEmbeddingExtractor
import com.k2fsa.sherpa.onnx.SpeakerEmbeddingExtractorConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import com.tingji.app.engine.AudioTools
import com.tingji.app.engine.SpeakerAnalyzer
import com.tingji.app.engine.SpeakerTurn
import com.tingji.app.engine.TaskCancelled
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.zip.CRC32
import kotlin.math.ceil
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

/** Streaming local VAD, resumable embeddings, one bounded global clustering. */
class SherpaSpeakerAnalyzer(private val context: Context) : SpeakerAnalyzer {
    private data class Region(val start: Long, var end: Long)
    private data class Window(val start: Long, val end: Long, val region: Int)
    private val rate = 16000
    private val version = "mobile-silero-campplus-global-v1"

    override fun analyze(wavFile: File, workDir: File, numSpeakers: Int, cancelled: () -> Boolean,
                         onProgress: (Int, Int, String) -> Unit): List<SpeakerTurn> {
        require(numSpeakers in 0..20) { "人数应为0至20。" }
        require(workDir.canonicalPath.startsWith(context.filesDir.canonicalPath + File.separator))
        workDir.mkdirs()
        val info = AudioTools.wavInfo(wavFile)
        require(info.sampleRate == rate && info.channels == 1 && info.bits == 16 && info.frames > 0)
        val signature = hash("$version:${wavFile.canonicalPath}:${wavFile.length()}:${wavFile.lastModified()}:${info.frames}")
        val regions = detectSpeech(wavFile, workDir, info, signature, cancelled, onProgress)
        require(regions.isNotEmpty()) { "未检测到清晰发言，请检查录音音量。" }
        val windows = makeWindows(regions)
        require(windows.size <= 250_000) { "录音过长，请分成多个任务。" }
        val windowSignature = hash(signature + regions.joinToString { "${it.start}:${it.end}" })
        val cache = File(workDir, "speaker-embeddings.bin")
        val extractor = SpeakerEmbeddingExtractor(context.assets,
            SpeakerEmbeddingExtractorConfig(model = "models/speaker_embedding.onnx", numThreads = 2, provider = "cpu", debug = false))
        try {
            val dim = extractor.dim()
            require(dim in 16..2048) { "人物模型输出无效。" }
            FeatureFile(cache, windowSignature, dim, windows.size).use { features ->
                RandomAccessFile(wavFile, "r").use { input ->
                    for (index in features.completed until windows.size) {
                        checkCancel(cancelled)
                        val w = windows[index]
                        var samples = readSamples(input, info.offset, w.start, (w.end - w.start).toInt())
                        if (samples.size < 6400) {
                            val original = samples
                            samples = FloatArray(6400) { original[it % original.size] }
                        }
                        val stream = extractor.createStream()
                        try {
                            stream.acceptWaveform(samples, rate)
                            stream.inputFinished()
                            require(extractor.isReady(stream)) { "声音片段过短，无法分析。" }
                            val vector = extractor.compute(stream)
                            require(vector.size == dim && vector.all { it.isFinite() }) { "人物声音特征无效。" }
                            features.append(SphericalClusterer.normalize(vector))
                        } finally { stream.release() }
                        if (index % 8 == 0 || index == windows.lastIndex) {
                            onProgress(index + 1, windows.size, "正在本地分析人物：${index + 1}/${windows.size}")
                        }
                    }
                }
                features.sync()
                checkCancel(cancelled)
                onProgress(windows.size, windows.size, "正在统一整场人物编号…")
                val trainingCount = min(1800, windows.size)
                val training = List(trainingCount) { i -> features.vector(if (trainingCount == 1) 0 else (i.toLong() * (windows.size - 1) / (trainingCount - 1)).toInt()) }
                val clusters = SphericalClusterer.fit(training, numSpeakers, cancelled)
                val labels = IntArray(windows.size)
                val uncertain = BooleanArray(windows.size)
                val similarCenters = clusters.centers.indices.any { a -> clusters.centers.indices.any { b -> a < b && SphericalClusterer.dot(clusters.centers[a], clusters.centers[b]) > .85f } }
                for (i in windows.indices) {
                    if (i % 50 == 0) checkCancel(cancelled)
                    val vector = features.vector(i)
                    val scores = clusters.centers.map { SphericalClusterer.dot(vector, it) }
                    val winner = scores.indices.maxBy { scores[it] }
                    val second = scores.filterIndexed { index, _ -> index != winner }.maxOrNull() ?: -1f
                    labels[i] = winner
                    uncertain[i] = numSpeakers == 0 || similarCenters || scores[winner] < .48f || scores[winner] - second < .09f || windows[i].end - windows[i].start < 12800
                }
                return makeTurns(regions, windows, labels, uncertain)
            }
        } finally { extractor.release() }
    }

    private fun detectSpeech(wav: File, work: File, info: AudioTools.WavInfo, signature: String,
                             cancelled: () -> Boolean, progress: (Int, Int, String) -> Unit): List<Region> {
        val checkpoint = File(work, "speech-regions.json")
        var next = 0L
        val regions = mutableListOf<Region>()
        if (checkpoint.isFile) runCatching {
            val old = JSONObject(checkpoint.readText())
            if (old.optString("signature") == signature) {
                val rows = old.getJSONArray("regions")
                require(rows.length() <= 250_000)
                for (i in 0 until rows.length()) {
                    val pair = rows.getJSONArray(i)
                    val start = pair.getLong(0); val end = pair.getLong(1)
                    require(start >= 0 && end > start && end <= info.frames)
                    regions += Region(start, end)
                }
                next = old.getLong("next_frame")
                require(next in 0..info.frames)
            }
        }.onFailure { next = 0; regions.clear() }
        if (next >= info.frames) return merged(regions)
        val vad = Vad(context.assets, VadModelConfig(
            sileroVadModelConfig = SileroVadModelConfig(model = "models/silero_vad.onnx", threshold = .45f,
                minSilenceDuration = .25f, minSpeechDuration = .12f, maxSpeechDuration = 30f),
            sampleRate = rate, numThreads = 2, provider = "cpu", debug = false))
        try {
            RandomAccessFile(wav, "r").use { input ->
                val block = rate * 180L
                while (next < info.frames) {
                    checkCancel(cancelled)
                    vad.reset()
                    val coreStart = next
                    val coreEnd = min(info.frames, coreStart + block)
                    val readStart = maxOf(0, coreStart - rate * 2L)
                    val readEnd = min(info.frames, coreEnd + rate * 2L)
                    fun drain() {
                        while (!vad.empty()) {
                            val speech = vad.front()
                            val start = maxOf(coreStart, readStart + speech.start)
                            val end = minOf(coreEnd, readStart + speech.start + speech.samples.size)
                            if (end > start) regions += Region(start, end)
                            vad.pop()
                        }
                    }
                    var frame = readStart
                    while (frame < readEnd) {
                        checkCancel(cancelled)
                        val count = min(8192L, readEnd - frame).toInt()
                        vad.acceptWaveform(readSamples(input, info.offset, frame, count))
                        drain()
                        frame += count
                    }
                    vad.flush(); drain()
                    next = coreEnd
                    val compact = merged(regions)
                    regions.clear(); regions.addAll(compact)
                    val array = JSONArray()
                    for (region in regions) array.put(JSONArray().put(region.start).put(region.end))
                    writeCheckpoint(checkpoint, JSONObject().put("signature", signature).put("next_frame", next).put("regions", array))
                    progress((next * 100 / info.frames).toInt(), 100, "正在检测发言位置：${next * 100 / info.frames}%")
                }
            }
        } finally { vad.release() }
        return merged(regions)
    }

    private fun merged(regions: List<Region>): List<Region> {
        val output = mutableListOf<Region>()
        for (region in regions.sortedBy { it.start }) {
            val previous = output.lastOrNull()
            if (previous != null && region.start <= previous.end) previous.end = maxOf(previous.end, region.end)
            else output += Region(region.start, region.end)
        }
        return output
    }

    private fun makeWindows(regions: List<Region>): List<Window> {
        val result = mutableListOf<Window>()
        val width = 28800L; val hop = 14400L
        for ((regionIndex, r) in regions.withIndex()) {
            if (r.end - r.start <= width) result += Window(r.start, r.end, regionIndex)
            else {
                var start = r.start
                while (start + width <= r.end) { result += Window(start, start + width, regionIndex); start += hop }
                if (result.last().end < r.end) result += Window(r.end - width, r.end, regionIndex)
            }
        }
        return result
    }

    private fun makeTurns(regions: List<Region>, windows: List<Window>, labels: IntArray, uncertain: BooleanArray): List<SpeakerTurn> {
        data class Raw(var start: Long, var end: Long, val label: Int, var uncertain: Boolean)
        val raw = mutableListOf<Raw>()
        var cursor = 0
        for ((index, region) in regions.withIndex()) {
            var left = region.start
            while (cursor < windows.size && windows[cursor].region == index) {
                val w = windows[cursor]
                val right = if (cursor + 1 < windows.size && windows[cursor + 1].region == index) {
                    val next = windows[cursor + 1]
                    (w.start + w.end + next.start + next.end) / 4
                } else region.end
                val previous = raw.lastOrNull()
                if (right > left) {
                    if (previous != null && previous.label == labels[cursor] && left - previous.end <= rate) {
                        previous.end = right; previous.uncertain = previous.uncertain || uncertain[cursor]
                    } else raw += Raw(left, right, labels[cursor], uncertain[cursor])
                }
                left = right; cursor++
            }
        }
        val ids = linkedMapOf<Int, String>()
        val turns = mutableListOf<SpeakerTurn>()
        for (r in raw) {
            val speaker = ids.getOrPut(r.label) { "S${ids.size + 1}" }
            var left = r.start
            while (left < r.end) {
                val end = min(r.end, left + 110L * rate)
                turns += SpeakerTurn(left.toDouble() / rate, end.toDouble() / rate, speaker,
                    r.uncertain || end - left < 12800)
                left = end
            }
        }
        return turns
    }

    private fun readSamples(input: RandomAccessFile, offset: Long, frame: Long, count: Int): FloatArray {
        require(count in 1..(rate * 190))
        val bytes = ByteArray(count * 2)
        input.seek(offset + frame * 2); input.readFully(bytes)
        return FloatArray(count) { i ->
            ((bytes[i * 2].toInt() and 255) or (bytes[i * 2 + 1].toInt() shl 8)).toShort() / 32768f
        }
    }

    private fun writeCheckpoint(file: File, value: JSONObject) {
        val atomic = AtomicFile(file); val out = atomic.startWrite()
        try { out.write(value.toString().toByteArray()); atomic.finishWrite(out) }
        catch (e: Exception) { atomic.failWrite(out); throw e }
    }
    private fun hash(text: String) = MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).joinToString("") { "%02x".format(it) }
    private fun checkCancel(cancelled: () -> Boolean) { if (cancelled()) throw TaskCancelled() }

    private class FeatureFile(file: File, signature: String, private val dimension: Int, private val total: Int) : AutoCloseable {
        private val storage = RandomAccessFile(file, "rw")
        private val recordSize = dimension * 4 + 8
        private var headerSize = 0L
        var completed = 0
            private set
        init {
            val valid = runCatching {
                require(storage.readUTF() == "tingji-features-v1" && storage.readUTF() == signature)
                require(storage.readInt() == dimension && storage.readInt() == total)
                headerSize = storage.filePointer
                val count = min(total.toLong(), (storage.length() - headerSize) / recordSize).toInt()
                for (i in 0 until count) {
                    storage.seek(headerSize + i.toLong() * recordSize)
                    val bytes = ByteArray(dimension * 4 + 4); storage.readFully(bytes)
                    val crc = storage.readInt()
                    require(CRC32().apply { update(bytes) }.value.toInt() == crc)
                    require(DataInputStream(ByteArrayInputStream(bytes)).readInt() == i)
                    completed++
                }
                true
            }.getOrDefault(false)
            if (!valid && headerSize == 0L) {
                storage.setLength(0); storage.writeUTF("tingji-features-v1"); storage.writeUTF(signature)
                storage.writeInt(dimension); storage.writeInt(total); headerSize = storage.filePointer; completed = 0
            }
            storage.setLength(headerSize + completed.toLong() * recordSize)
        }
        fun append(vector: FloatArray) {
            require(completed < total && vector.size == dimension)
            val buffer = ByteArrayOutputStream()
            DataOutputStream(buffer).use { out -> out.writeInt(completed); vector.forEach(out::writeFloat) }
            val bytes = buffer.toByteArray()
            storage.seek(headerSize + completed.toLong() * recordSize)
            storage.write(bytes); storage.writeInt(CRC32().apply { update(bytes) }.value.toInt()); completed++
            if (completed % 20 == 0) sync()
        }
        fun vector(index: Int): FloatArray {
            require(index in 0 until completed)
            storage.seek(headerSize + index.toLong() * recordSize + 4)
            return FloatArray(dimension) { storage.readFloat() }
        }
        fun sync() = storage.fd.sync()
        override fun close() = storage.close()
    }
}

internal object SphericalClusterer {
    data class Model(val centers: List<FloatArray>, val loss: Double)
    fun dot(a: FloatArray, b: FloatArray): Float { var sum = 0f; for (i in a.indices) sum += a[i] * b[i]; return sum }
    fun normalize(vector: FloatArray): FloatArray {
        val norm = sqrt(vector.sumOf { it.toDouble() * it })
        require(norm > 1e-9 && norm.isFinite()) { "未提取到有效声音特征，请检查音量。" }
        return FloatArray(vector.size) { (vector[it] / norm).toFloat() }
    }
    fun fit(points: List<FloatArray>, requested: Int, cancelled: () -> Boolean = { false }): Model {
        require(points.isNotEmpty() && points.size <= 1800 && requested in 0..20)
        require(requested <= points.size) { "有效发言太少，无法区分填写的人数。" }
        if (requested > 0) return fitCount(points, requested, cancelled)
        val one = fitCount(points, 1, cancelled)
        if (points.size < 4 || one.loss / points.size < .10) return one
        var best = one; var bestScore = 0.0
        for (count in 2..min(8, maxOf(2, points.size / 4))) {
            val model = fitCount(points, count, cancelled)
            val labels = points.map { p -> model.centers.indices.maxBy { dot(p, model.centers[it]) } }
            val populations = IntArray(count); labels.forEach { populations[it]++ }
            if (populations.any { it < 2 }) continue
            var score = 0.0; var measured = 0
            val stride = maxOf(1, points.size / 240)
            for (i in points.indices step stride) {
                val distances = DoubleArray(count); val samples = IntArray(count)
                for (j in points.indices step stride) if (i != j) {
                    distances[labels[j]] += 1.0 - dot(points[i], points[j]).coerceIn(-1f, 1f); samples[labels[j]]++
                }
                if (samples[labels[i]] == 0) continue
                val own = distances[labels[i]] / samples[labels[i]]
                val other = (0 until count).filter { it != labels[i] && samples[it] > 0 }.minOfOrNull { distances[it] / samples[it] } ?: continue
                score += (other - own) / maxOf(own, other, 1e-6); measured++
            }
            val adjusted = score / maxOf(1, measured) - .025 * (count - 2)
            if (adjusted > maxOf(.20, bestScore)) { best = model; bestScore = adjusted }
        }
        return best
    }
    private fun fitCount(points: List<FloatArray>, count: Int, cancelled: () -> Boolean): Model {
        var best: Model? = null
        for (attempt in 0..2) {
            val random = java.util.Random(1901L + attempt)
            val centers = mutableListOf(points[random.nextInt(points.size)].copyOf())
            while (centers.size < count) {
                val distances = points.map { p -> (1.0 - centers.maxOf { dot(p, it).toDouble() }).coerceAtLeast(0.0) }
                require(distances.sum() > 1e-7) { "当前声音过于相似，无法可靠分成填写的人数。" }
                var threshold = random.nextDouble() * distances.sum()
                var choice = points.lastIndex
                for (i in distances.indices) { threshold -= distances[i]; if (threshold <= 0) { choice = i; break } }
                centers += points[choice].copyOf()
            }
            val labels = IntArray(points.size) { -1 }
            repeat(35) {
                if (cancelled()) throw TaskCancelled()
                val sums = List(count) { FloatArray(points[0].size) }; val populations = IntArray(count)
                for ((i, p) in points.withIndex()) {
                    val label = centers.indices.maxBy { dot(p, centers[it]) }; labels[i] = label; populations[label]++
                    for (dimension in p.indices) sums[label][dimension] += p[dimension]
                }
                for (label in centers.indices) centers[label] = if (populations[label] > 0) normalize(sums[label]) else points[random.nextInt(points.size)].copyOf()
            }
            val loss = points.sumOf { p -> 1.0 - centers.maxOf { dot(p, it).toDouble() } }
            if (best == null || loss < best!!.loss) best = Model(centers.map { it.copyOf() }, loss)
        }
        return best!!
    }
}
