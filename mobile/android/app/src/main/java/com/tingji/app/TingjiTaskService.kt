package com.tingji.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/** User-started, visible work. Durable engine checkpoints survive process loss. */
class TingjiTaskService : Service() {
    private val host get() = application as TingjiApplication
    private var wakeLock: PowerManager.WakeLock? = null
    private var timedOut = false
    private var foreground = false
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        current = this
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "录音处理进度", NotificationManager.IMPORTANCE_LOW).apply {
                description = "显示人物分析、转写和总结进度，可随时停止。"
            })
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_CANCEL) {
            host.cancelAll()
            refresh()
            return START_NOT_STICKY
        }
        showForeground(notification("正在准备录音任务…", 0, 0, true))
        intent?.getStringExtra("request_id")?.let(host::executePending)
        return START_NOT_STICKY
    }

    private fun showForeground(value: Notification) {
        if (Build.VERSION.SDK_INT >= 35) {
            startForeground(NOTIFICATION_ID, value,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROCESSING)
        } else if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, value, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else startForeground(NOTIFICATION_ID, value)
        foreground = true
        if (wakeLock?.isHeld != true) {
            wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Tingji:RecordingTask").apply { acquire(30 * 60 * 1000L) }
        }
    }

    fun refresh() {
        if (timedOut) return
        val task = host.engine.taskState()
        val running = task?.optBoolean("running", false) == true || host.activeShellOperations > 0
        if (!running) {
            val completedForegroundTask = foreground
            if (foreground) stopForeground(STOP_FOREGROUND_REMOVE)
            foreground = false
            releaseWakeLock()
            if (completedForegroundTask && notificationsAllowed()) {
                val title = if (task.optString("stage") in setOf("error", "partial", "cancelled")) "听记任务已暂停" else "听记录音处理已结束"
                getSystemService(NotificationManager::class.java).notify(COMPLETION_ID,
                    NotificationCompat.Builder(this, CHANNEL)
                        .setSmallIcon(R.drawable.ic_notification).setContentTitle(title)
                        .setContentText(task.optString("message", "打开听记查看结果。"))
                        .setContentIntent(PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
                        .setAutoCancel(true).setOnlyAlertOnce(true).build())
            }
            stopSelf()
            return
        }
        val message = task?.optString("message")?.takeIf { it.isNotBlank() } ?: "正在导入录音…"
        val current = task?.optInt("current", 0) ?: 0
        val total = task?.optInt("total", 0) ?: 0
        if (notificationsAllowed()) getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(message, current, total, total <= 0))
        else showForeground(notification(message, current, total, total <= 0))
        if (wakeLock?.isHeld != true) showForeground(notification(message, current, total, total <= 0))
    }

    private fun notificationsAllowed() = Build.VERSION.SDK_INT < 33 ||
        androidx.core.content.ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS) == android.content.pm.PackageManager.PERMISSION_GRANTED

    override fun onTimeout(startId: Int, fgsType: Int) {
        timedOut = true
        host.cancelAll()
        releaseWakeLock()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun notification(message: String, current: Int, total: Int, indeterminate: Boolean): Notification {
        val activity = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val cancel = PendingIntent.getService(this, 1, Intent(this, TingjiTaskService::class.java).setAction(ACTION_CANCEL), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("听记正在处理录音")
            .setContentText(message.take(200))
            .setContentIntent(activity)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setProgress(total.coerceAtLeast(0), current.coerceAtLeast(0), indeterminate)
            .addAction(0, "停止并保留进度", cancel)
            .build()
    }

    private fun releaseWakeLock() {
        if (wakeLock?.isHeld == true) wakeLock?.release()
        wakeLock = null
    }

    override fun onDestroy() {
        if (current === this) current = null
        releaseWakeLock()
        super.onDestroy()
    }

    companion object {
        const val CHANNEL = "recording_tasks"
        const val NOTIFICATION_ID = 2101
        const val COMPLETION_ID = 2102
        const val ACTION_CANCEL = "com.tingji.app.STOP_TASK"
        @Volatile var current: TingjiTaskService? = null
            private set
    }
}
