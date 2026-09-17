package com.yikou.lightfood

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import java.util.concurrent.Executors

/**
 * 前台 Service：持有 Python 进程并在息屏/切后台时维持优先级。
 *
 * HTTP 服务在 Chaquopy Python 线程里运行；Service 被杀后由用户再次打开 App 恢复，
 * 任务状态和桥接事件由现有 Bridge 落盘 + 重放机制保证。
 */
class TaskService : Service() {
    private val executor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "yikou-startup")
    }

    @Volatile
    private var started = false

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startForegroundCompat(buildNotification(getString(R.string.notification_task_starting)))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!started) {
            started = true
            executor.execute { startPython() }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        executor.shutdownNow()
        super.onDestroy()
    }

    private fun startPython() {
        try {
            val info = PythonRuntime(applicationContext).start()
            RuntimeState.serverReady(info.url, info.port, info.wpsMessage)
            val message = if (info.wpsOk) getString(R.string.notification_task_running)
                else "服务已启动；${info.wpsMessage}"
            notify(buildNotification(message))
        } catch (exc: Throwable) {
            val diagnostics = PythonRuntime(applicationContext).diagnostics()
            RuntimeState.error(
                "${exc.javaClass.simpleName}: ${exc.message}",
                diagnostics,
            )
            notify(buildNotification(
                getString(R.string.notification_task_error), title = getString(R.string.app_name)))
        }
    }

    private fun startForegroundCompat(notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun notify(notification: Notification) {
        val manager = getSystemService(NotificationManager::class.java)
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(message: String, title: String = getString(R.string.app_name)):
        Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_TASKS)
            .setContentTitle(title)
            .setContentText(message)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
    }

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_TASKS,
                getString(R.string.notification_channel_tasks),
                NotificationManager.IMPORTANCE_LOW,
            )
        )
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_CAPTCHA,
                getString(R.string.notification_channel_captcha),
                NotificationManager.IMPORTANCE_HIGH,
            )
        )
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ADDRESS,
                getString(R.string.notification_channel_address),
                NotificationManager.IMPORTANCE_HIGH,
            )
        )
    }

    companion object {
        const val CHANNEL_TASKS = "yikou_tasks"
        const val CHANNEL_CAPTCHA = "yikou_captcha"
        const val CHANNEL_ADDRESS = "yikou_address"
        private const val NOTIFICATION_ID = 8756

        fun start(context: Context) {
            context.startForegroundService(Intent(context, TaskService::class.java))
        }
    }
}
