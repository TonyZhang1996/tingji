package com.tingji.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewAssetLoader
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : ComponentActivity() {
    private val host get() = application as TingjiApplication
    private lateinit var webView: WebView
    private var pickerRequest: String? = null
    private val importing = AtomicBoolean(false)
    private var destroyed = false
    private var sharedAudio: Uri? = null
    private var webReady = false
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private val pickAudio = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        val request = pickerRequest
        pickerRequest = null
        if (uri == null) request?.let { respond(it, Result.success(host.engine.state())) }
        else importAudio(uri, request)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        webView = WebView(this)
        webView.isSaveEnabled = false
        if (Build.VERSION.SDK_INT >= 26) webView.importantForAutofill = WebView.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        val container = FrameLayout(this)
        container.addView(webView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(container)
        ViewCompat.setOnApplyWindowInsetsListener(container) { view, insets ->
            val safe = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(safe.left, safe.top, safe.right, maxOf(safe.bottom, keyboard.bottom))
            insets
        }
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = false
            allowFileAccess = false
            allowContentAccess = false
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
            mediaPlaybackRequiresUserGesture = true
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
        }
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        webView.addJavascriptInterface(AndroidBridge(), "TingjiAndroid")
        webView.webChromeClient = WebChromeClient()
        val assets = WebViewAssetLoader.Builder().addPathHandler("/") { path ->
            if (path.contains("..") || !path.matches(Regex("[A-Za-z0-9_./-]+"))) null
            else try {
                val actual = if (path.isEmpty()) "index.html" else path
                val mime = when {
                    actual.endsWith(".html") -> "text/html"
                    actual.endsWith(".js") -> "application/javascript"
                    actual.endsWith(".css") -> "text/css"
                    actual.endsWith(".png") -> "image/png"
                    actual.endsWith(".svg") -> "image/svg+xml"
                    else -> "application/octet-stream"
                }
                WebResourceResponse(mime, "UTF-8", 200, "OK", securityHeaders(), this.assets.open("web/$actual"))
            } catch (_: Exception) { blockedResponse(404) }
        }.build()
        webView.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val uri = request.url
                if (uri.scheme != "https" || uri.host != ORIGIN_HOST) return blockedResponse(403)
                if (uri.path?.startsWith("/media/") == true) {
                    val range = request.requestHeaders.entries.firstOrNull { it.key.equals("Range", ignoreCase = true) }?.value
                    return host.media.response(uri.path!!.removePrefix("/media/"), range)
                }
                return assets.shouldInterceptRequest(uri) ?: blockedResponse(404)
            }
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                if (uri.scheme == "https" && uri.host == ORIGIN_HOST && uri.path == "/index.html") return false
                if (request.isForMainFrame && uri.scheme in setOf("https", "http")) {
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                }
                return true
            }
            override fun onPageFinished(view: WebView, url: String) {
                webReady = Uri.parse(url).let { it.scheme == "https" && it.host == ORIGIN_HOST && it.path == "/index.html" }
                if (webReady) {
                    sharedAudio?.let { uri -> sharedAudio = null; importAudio(uri, null) }
                    if (BuildConfig.DEBUG && intent.getBooleanExtra("run_self_check", false)) {
                        intent.removeExtra("run_self_check")
                        runDebugSelfCheck()
                    }
                }
            }
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                webView.evaluateJavascript("typeof window.__TINGJI_ANDROID_BACK__==='function'&&window.__TINGJI_ANDROID_BACK__()") { consumed ->
                    if (consumed != "true") moveTaskToBack(true)
                }
            }
        })
        receiveSharedAudio(intent)
        webView.loadUrl("https://$ORIGIN_HOST/index.html")
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        receiveSharedAudio(intent)
        if (webReady) sharedAudio?.let { uri -> sharedAudio = null; importAudio(uri, null) }
    }

    private fun receiveSharedAudio(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val uri = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_STREAM)
        if (uri?.scheme == "content") sharedAudio = uri
    }

    private inner class AndroidBridge {
        @JavascriptInterface
        fun postMessage(raw: String) {
            if (raw.length > 1024 * 1024) return
            val message = try { JSONObject(raw) } catch (_: Exception) { return }
            val id = message.optString("id").take(80)
            val action = message.optString("action")
            val payload = message.optJSONObject("payload") ?: JSONObject()
            if (id.isEmpty() || !action.matches(Regex("[a-z_]{1,40}"))) return
            runOnUiThread {
                if (destroyed) return@runOnUiThread
                val origin = Uri.parse(webView.url ?: "")
                if (origin.scheme != "https" || origin.host != ORIGIN_HOST || origin.path != "/index.html") return@runOnUiThread
                when (action) {
                    "choose_audio" -> {
                        if (pickerRequest != null || importing.get()) respond(id, Result.failure(IllegalStateException("正在导入录音，请稍候。")))
                        else if (host.engine.taskState().optBoolean("running")) respond(id, Result.failure(IllegalStateException("请先停止当前任务，再导入录音。")))
                        else {
                            pickerRequest = id
                            pickAudio.launch(arrayOf("audio/*", "video/*", "application/octet-stream"))
                        }
                    }
                    "choose_output" -> respond(id, Result.failure(IllegalStateException("安卓任务保存在听记私有目录；请通过导出和分享保存到其它位置。")))
                    "copy_text" -> {
                        val text = payload.optString("text")
                        if (text.length > 2_000_000) respond(id, Result.failure(IllegalStateException("文字过长，请导出文件。")))
                        else {
                            (getSystemService(CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("听记文字", text))
                            respond(id, Result.success(host.engine.state()))
                        }
                    }
                    "open_summary", "open_output" -> host.io.execute {
                        val result = runCatching {
                            val file = if (action == "open_summary") host.engine.summaryFile()
                                else host.engine.exportFile(payload.optString("format", "txt"))
                            require(file != null && file.isFile) { "结果尚未生成。" }
                            shareExport(file)
                            host.engine.state()
                        }
                        respond(id, result)
                    }
                    "analyze", "transcribe", "summarize" -> host.foregroundOperation(
                        { host.engine.dispatch(action, payload) }, { respond(id, it) })
                    else -> host.io.execute {
                        val result = runCatching { host.engine.dispatch(action, payload) }
                        if (action == "config" && result.isSuccess) runOnUiThread {
                            if (!destroyed) {
                                webView.clearFocus()
                                WindowCompat.getInsetsController(window, webView).hide(WindowInsetsCompat.Type.ime())
                            }
                        }
                        respond(id, result)
                    }
                }
            }
        }
    }

    private fun respond(id: String, result: Result<JSONObject>) {
        val body = result.fold(
            { JSONObject().put("ok", true).put("state", it) },
            { failure -> JSONObject().put("ok", false).put("message", safeError(failure)) })
        runOnUiThread {
            val origin = Uri.parse(webView.url ?: "")
            if (!destroyed && origin.scheme == "https" && origin.host == ORIGIN_HOST && origin.path == "/index.html") webView.evaluateJavascript(
                "window.__TINGJI_ANDROID_RESULT__&&window.__TINGJI_ANDROID_RESULT__(${JSONObject.quote(id)},$body)", null)
        }
    }

    private fun safeError(error: Throwable): String {
        // Only validation messages are shown. Arbitrary library exceptions may
        // contain local paths, request information or sensitive headers.
        return if (error is IllegalArgumentException || error is IllegalStateException) {
            val message = error.message ?: "操作未完成，请重试。"
            if (message.length <= 300 && !message.contains(Regex("(?i)(sk-|bearer |api[_-]?key=|https?://)"))) message
            else "操作未完成，已保存的进度会保留。"
        } else "操作未完成，请检查录音文件、网络或剩余空间。"
    }

    private fun importAudio(uri: Uri, requestId: String?) {
        if (uri.scheme != "content" || !importing.compareAndSet(false, true)) return
        if (host.engine.taskState().optBoolean("running")) {
            importing.set(false)
            requestId?.let { respond(it, Result.failure(IllegalStateException("请先停止当前任务，再导入录音。"))) }
            return
        }
        host.foregroundOperation({
            var incoming: File? = null
            try {
                var displayName = "录音"
                var advertisedSize = -1L
                contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        displayName = cursor.getString(0)?.take(180) ?: displayName
                        if (!cursor.isNull(1)) advertisedSize = cursor.getLong(1)
                    }
                }
                require(advertisedSize <= MAX_IMPORT_BYTES) { "录音文件过大，请先分成较小的文件。" }
                val directory = File(filesDir, "imports").apply { mkdirs() }
                if (advertisedSize > 0) require(directory.usableSpace > advertisedSize + 256 * 1024 * 1024L) { "手机剩余空间不足，请清理缓存后重试。" }
                val extension = displayName.substringAfterLast('.', "audio").lowercase().takeIf { it.matches(Regex("[a-z0-9]{1,8}")) } ?: "audio"
                val finalFile = File(directory, "${UUID.randomUUID()}.$extension")
                incoming = File(directory, finalFile.name + ".part")
                contentResolver.openInputStream(uri)?.use { input ->
                    incoming!!.outputStream().use { output ->
                        val buffer = ByteArray(128 * 1024)
                        var total = 0L
                        while (true) {
                            if (host.shellCancelRequested.get()) throw IllegalStateException("录音导入已停止，原始文件会保留。")
                            val count = input.read(buffer)
                            if (count < 0) break
                            total += count
                            require(total <= MAX_IMPORT_BYTES) { "录音文件过大，请先分成较小的文件。" }
                            output.write(buffer, 0, count)
                        }
                        require(total > 0) { "录音文件为空。" }
                        output.fd.sync()
                    }
                } ?: throw IllegalArgumentException("无法读取录音，请从文件管理器重新选择。")
                require(incoming!!.renameTo(finalFile)) { "录音保存失败，请检查手机剩余空间。" }
                incoming = finalFile
                val state = host.engine.acceptAudio(finalFile, displayName)
                if (finalFile.exists()) finalFile.delete()
                incoming = null
                state
            } finally { incoming?.delete() }
        }) { result ->
            importing.set(false)
            if (requestId != null) respond(requestId, result)
            else result.onFailure { Toast.makeText(this, safeError(it), Toast.LENGTH_LONG).show() }
        }
    }

    private fun shareExport(file: File) {
        val canonical = file.canonicalFile
        val jobId = canonical.parentFile?.name.orEmpty()
        val jobs = File(host.engine.dataRoot, "jobs").canonicalFile
        require(jobId.matches(Regex("[a-f0-9]{32}")) && canonical.parentFile?.parentFile == jobs) { "无法分享此文件。" }
        val sharing = File(cacheDir, "shared_exports").canonicalFile
        val jobSharing = File(sharing, jobId)
        val snapshot = File(jobSharing, UUID.randomUUID().toString())
        require(snapshot.canonicalPath.startsWith(sharing.path + File.separator)) { "无法分享此文件。" }
        val safeName = canonical.name.replace(Regex("[^\\p{L}\\p{N}_.-]"), "_").take(150)
        val copy = File(snapshot, safeName)
        val uri = try {
            require(snapshot.mkdirs()) { "导出文件创建失败。" }
            canonical.copyTo(copy, overwrite = false)
            FileProvider.getUriForFile(this, "$packageName.files", copy)
        } catch (error: Exception) {
            copy.delete()
            snapshot.delete()
            jobSharing.delete() // Only succeeds when this job has no other snapshots.
            throw error
        }
        runOnUiThread {
            try {
                startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                    type = MediaRegistry.mime(copy.extension)
                    putExtra(Intent.EXTRA_STREAM, uri)
                    clipData = ClipData.newUri(contentResolver, safeName, uri)
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }, "导出听记结果"))
            } catch (_: Exception) {
                copy.delete()
                snapshot.delete()
                jobSharing.delete()
                Toast.makeText(this, "无法打开分享，请重试。", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun runDebugSelfCheck() {
        if (!BuildConfig.DEBUG) return
        val qa = File(filesDir, "qa").apply { mkdirs() }
        val input = File(qa, "input.wav")
        host.foregroundOperation({
            require(input.isFile) { "请先放入仅用于测试的 qa/input.wav。" }
            host.engine.acceptAudio(input, "虚构合成测试录音.wav")
            host.engine.dispatch("analyze", JSONObject())
        }) { result ->
            if (result.isFailure) {
                File(qa, "self-check.json").writeText(JSONObject().put("ok", false).put("error", "analysis_start_failed").toString())
            } else pollDebugSelfCheck(qa, 0)
        }
    }

    private fun pollDebugSelfCheck(directory: File, attempts: Int) {
        if (!BuildConfig.DEBUG) return
        val state = host.engine.state()
        if (!state.getJSONObject("task").optBoolean("running")) {
            val task = state.getJSONObject("task")
            val ok = task.optString("stage") !in setOf("error", "cancelled", "partial")
            File(directory, "self-check.json").writeText(JSONObject().put("ok", ok).put("state", state).toString())
        } else if (attempts < 7200) host.main.postDelayed({ pollDebugSelfCheck(directory, attempts + 1) }, 500)
    }

    override fun onDestroy() {
        destroyed = true
        webView.removeJavascriptInterface("TingjiAndroid")
        webView.stopLoading()
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        const val ORIGIN_HOST = "appassets.androidplatform.net"
        private const val MAX_IMPORT_BYTES = 8L * 1024 * 1024 * 1024
        private fun securityHeaders() = mapOf(
            "Cache-Control" to "no-store", "X-Content-Type-Options" to "nosniff",
            "Content-Security-Policy" to "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; media-src 'self'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'")
        private fun blockedResponse(status: Int) = WebResourceResponse("text/plain", "UTF-8", status,
            if (status == 403) "Forbidden" else "Not Found", mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(ByteArray(0)))
    }
}
