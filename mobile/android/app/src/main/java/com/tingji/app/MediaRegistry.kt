package com.tingji.app

import android.content.Context
import android.webkit.WebResourceResponse
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileInputStream
import java.io.FilterInputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/** A private-file allowlist, never a filesystem path supplied by JavaScript. */
class MediaRegistry(context: Context) {
    private val roots = listOf(context.filesDir.canonicalFile, context.cacheDir.canonicalFile)
    private val files = ConcurrentHashMap<String, File>()

    fun url(file: File): String {
        val canonical = file.canonicalFile
        require(roots.any { canonical.path.startsWith(it.path + File.separator) }) { "录音不在听记的数据目录中。" }
        require(canonical.isFile) { "录音文件不存在。" }
        val identity = "${canonical.path}:${canonical.length()}:${canonical.lastModified()}"
        val id = MessageDigest.getInstance("SHA-256").digest(identity.toByteArray())
            .joinToString("") { "%02x".format(it) }.take(32)
        files[id] = canonical
        return "https://appassets.androidplatform.net/media/$id"
    }

    fun response(id: String, rangeHeader: String?): WebResourceResponse {
        if (!id.matches(Regex("[a-f0-9]{32}"))) return error(404)
        val file = files[id] ?: return error(404)
        val canonical = file.canonicalFile
        if (!canonical.isFile || !roots.any { canonical.path.startsWith(it.path + File.separator) }) return error(404)
        val size = canonical.length()
        val range = try { ByteRange.parse(rangeHeader, size) } catch (_: IllegalArgumentException) {
            return error(416, mapOf("Content-Range" to "bytes */$size"))
        }
        val partial = rangeHeader != null
        val headers = mutableMapOf("Accept-Ranges" to "bytes", "Cache-Control" to "no-store",
            "X-Content-Type-Options" to "nosniff", "Content-Length" to range.length.toString())
        if (partial) headers["Content-Range"] = "bytes ${range.start}-${range.end}/$size"
        val stream = FileInputStream(canonical)
        // Chromium's Android InputStreamReader validates Range against
        // available(), then skips its start BEFORE publishing our headers.
        // Give it the full prefix so that the range is not applied twice.
        // For >2 GiB report unknown size instead of a saturated Java Int.
        val unknownSize = range.end >= Int.MAX_VALUE.toLong()
        // A suffix cannot be resolved by Chromium when available() is unknown.
        // It has no positive start to skip, so position that special case here.
        val suffixWithUnknownSize = unknownSize && rangeHeader?.trim()?.startsWith("bytes=-") == true
        val initialOffset = if (suffixWithUnknownSize) range.start else 0L
        if (initialOffset > 0) stream.channel.position(initialOffset)
        return WebResourceResponse(mime(canonical.extension), null, if (partial) 206 else 200,
            if (partial) "Partial Content" else "OK", headers, LimitedStream(stream, range.end + 1 - initialOffset, unknownSize))
    }

    private fun error(status: Int, extra: Map<String, String> = emptyMap()) =
        WebResourceResponse("text/plain", "UTF-8", status, if (status == 416) "Range Not Satisfiable" else "Not Found",
            mapOf("Cache-Control" to "no-store") + extra, ByteArrayInputStream(ByteArray(0)))

    companion object {
        fun mime(extension: String): String = when (extension.lowercase()) {
            "wav" -> "audio/wav"
            "mp3" -> "audio/mpeg"
            "m4a", "mp4" -> "audio/mp4"
            "aac" -> "audio/aac"
            "ogg", "opus" -> "audio/ogg"
            "flac" -> "audio/flac"
            "txt" -> "text/plain"
            "md" -> "text/markdown"
            "json" -> "application/json"
            "csv" -> "text/csv"
            "srt" -> "application/x-subrip"
            else -> "application/octet-stream"
        }
    }
}

internal data class ByteRange(val start: Long, val end: Long) {
    val length: Long get() = if (end < start) 0 else end - start + 1

    companion object {
        fun parse(header: String?, size: Long): ByteRange {
            require(size >= 0)
            if (header == null) return ByteRange(0, size - 1)
            require(size > 0)
            val match = Regex("bytes=(\\d*)-(\\d*)").matchEntire(header.trim()) ?: throw IllegalArgumentException()
            val left = match.groupValues[1]
            val right = match.groupValues[2]
            require(left.isNotEmpty() || right.isNotEmpty())
            if (left.isEmpty()) {
                val suffix = right.toLongOrNull() ?: throw IllegalArgumentException()
                require(suffix > 0)
                return ByteRange(maxOf(0, size - suffix), size - 1)
            }
            val start = left.toLongOrNull() ?: throw IllegalArgumentException()
            val end = if (right.isEmpty()) size - 1 else minOf(right.toLongOrNull() ?: throw IllegalArgumentException(), size - 1)
            require(start in 0 until size && end >= start)
            return ByteRange(start, end)
        }
    }
}

private class LimitedStream(source: InputStream, private var remaining: Long, private val unknownSize: Boolean = false) : FilterInputStream(source) {
    override fun available(): Int = if (unknownSize) 0 else minOf(remaining, `in`.available().toLong()).toInt()
    override fun skip(count: Long): Long {
        if (count <= 0 || remaining <= 0) return 0
        // Chromium's Java skip result is narrowed to a signed Int in JNI.
        // Its reader already loops until the full Long offset is reached.
        val skipped = `in`.skip(minOf(count, remaining, Int.MAX_VALUE.toLong()))
        remaining -= skipped
        return skipped
    }
    override fun read(): Int {
        if (remaining <= 0) return -1
        val value = super.read()
        if (value >= 0) remaining--
        return value
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (remaining <= 0) return -1
        val count = `in`.read(buffer, offset, minOf(length.toLong(), remaining).toInt())
        if (count > 0) remaining -= count
        return count
    }
}
