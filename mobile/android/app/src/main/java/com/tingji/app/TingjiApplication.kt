package com.tingji.app

import android.app.Application
import android.content.Intent
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import com.tingji.app.engine.MeetingEngine
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class TingjiApplication : Application() {
    val media by lazy { MediaRegistry(this) }
    val io = Executors.newSingleThreadExecutor()
    private val controls = Executors.newSingleThreadExecutor()
    val main = Handler(Looper.getMainLooper())
    val engine by lazy { MeetingEngine(this, media::url, ::taskChanged, SherpaSpeakerAnalyzer(this)) }

    private val pending = ConcurrentHashMap<String, PendingOperation>()
    val shellCancelRequested = AtomicBoolean(false)
    @Volatile var activeShellOperations = 0
        private set

    /** Intents carry only a random request ID; API credentials stay in memory. */
    fun foregroundOperation(block: () -> JSONObject, callback: (Result<JSONObject>) -> Unit) {
        val id = UUID.randomUUID().toString()
        pending[id] = PendingOperation(block, callback)
        try {
            ContextCompat.startForegroundService(this, Intent(this, TingjiTaskService::class.java).putExtra("request_id", id))
        } catch (_: RuntimeException) {
            pending.remove(id)
            callback(Result.failure(IllegalStateException("请保持听记在前台，再开始任务。")))
        }
    }

    fun executePending(id: String) {
        val operation = pending.remove(id) ?: return
        activeShellOperations++
        io.execute {
            shellCancelRequested.set(false)
            val result = runCatching(operation.block)
            main.post {
                activeShellOperations--
                operation.callback(result)
                taskChanged()
            }
        }
    }

    fun cancelAll() {
        shellCancelRequested.set(true)
        // Acquiring the engine lock must not delay Service.onTimeout's stop.
        controls.execute { engine.cancel() }
    }

    private fun taskChanged() {
        main.post { TingjiTaskService.current?.refresh() }
    }

    private data class PendingOperation(val block: () -> JSONObject, val callback: (Result<JSONObject>) -> Unit)
}
