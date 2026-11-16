package com.tingji.app.engine

import android.util.AtomicFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

internal fun nowIso(): String = Instant.now().toString()
internal fun cloneJson(value: JSONObject) = JSONObject(value.toString())
internal fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }
internal fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
internal fun jsonArray(values: Iterable<Any>): JSONArray = JSONArray().also { a -> values.forEach { a.put(it) } }
internal fun sha256(text: String): String = MessageDigest.getInstance("SHA-256")
    .digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
internal fun randomId(): String = UUID.randomUUID().toString().replace("-", "")
internal fun fileHash(file:File,cancelled:()->Boolean={false}):String {
    val digest=MessageDigest.getInstance("SHA-256")
    file.inputStream().use { input -> val buffer=ByteArray(65536);while(true) {if(cancelled())throw TaskCancelled();val n=input.read(buffer);if(n<0)break;digest.update(buffer,0,n)} }
    return digest.digest().joinToString("") {"%02x".format(it)}
}
internal fun safeName(text: String): String = text.replace(Regex("[\\\\/:*?\"<>|\\p{Cntrl}]"), "_")
    .trim().take(100).ifBlank { "会议录音" }
internal fun readJson(file: File): JSONObject? = try {
    // openRead restores a valid .bak on older Android versions after a crash.
    AtomicFile(file).openRead().use { stream ->
        if(file.length()<=32L*1024*1024)JSONObject(stream.reader(Charsets.UTF_8).readText()) else null
    }
} catch (_: Exception) { null }
internal fun writeJson(file: File, value: JSONObject) {
    file.parentFile?.mkdirs()
    val atomic = AtomicFile(file)
    val stream = atomic.startWrite()
    try {
        stream.write(value.toString().toByteArray(Charsets.UTF_8))
        atomic.finishWrite(stream)
    } catch (e: Exception) { atomic.failWrite(stream); throw e }
}
internal fun writeText(file: File, value: String) {
    file.parentFile?.mkdirs()
    val atomic = AtomicFile(file)
    val stream = atomic.startWrite()
    try { stream.write(value.toByteArray(Charsets.UTF_8)); atomic.finishWrite(stream) }
    catch (e: Exception) { atomic.failWrite(stream); throw e }
}
internal fun within(root: File, file: File): Boolean = file.canonicalPath.startsWith(root.canonicalPath + File.separator)
internal fun clock(seconds: Double, millis: Boolean = false): String {
    val total = (seconds.coerceAtLeast(0.0) * 1000).toLong()
    val h = total / 3600000; val m = total / 60000 % 60; val s = total / 1000 % 60
    return "%02d:%02d:%02d".format(h,m,s) + if (millis) ".%03d".format(total % 1000) else ""
}
