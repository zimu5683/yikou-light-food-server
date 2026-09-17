package com.yikou.lightfood

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent

/**
 * 等待交互时的前台通知：任务在后台需要用户输入验证码/确认地址。
 *
 * 不新增业务逻辑；Bridge 在抛出 captcha/address_input 事件时调用。
 */
object InteractionNotifier {
    const val CHANNEL_CAPTCHA = "yikou_captcha"
    const val CHANNEL_ADDRESS = "yikou_address"

    private const val NOTIFICATION_CAPTCHA = 8757
    private const val NOTIFICATION_ADDRESS = 8758

    @Volatile
    private var appContext: Context? = null

    @JvmStatic
    fun initialize(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        createChannel(ctx, CHANNEL_CAPTCHA, "等待验证码")
        createChannel(ctx, CHANNEL_ADDRESS, "等待地址确认")
    }

    @JvmStatic
    fun notify(kind: String): Boolean {
        val ctx = appContext ?: return false
        return when (kind) {
            "captcha" -> post(ctx, NOTIFICATION_CAPTCHA, CHANNEL_CAPTCHA,
                "需要输入验证码", "任务正在等待验证码，点击返回应用")
            "address" -> post(ctx, NOTIFICATION_ADDRESS, CHANNEL_ADDRESS,
                "需要确认地址", "任务正在等待地址确认，点击返回应用")
            else -> false
        }
    }

    @JvmStatic
    fun clear(): Boolean {
        val ctx = appContext ?: return false
        val manager = ctx.getSystemService(NotificationManager::class.java) ?: return false
        manager.cancel(NOTIFICATION_CAPTCHA)
        manager.cancel(NOTIFICATION_ADDRESS)
        return true
    }

    private fun post(
        context: Context,
        id: Int,
        channelId: String,
        title: String,
        text: String,
    ): Boolean {
        return try {
            val pendingIntent = PendingIntent.getActivity(
                context,
                id,
                Intent(context, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val notification = Notification.Builder(context, channelId)
                .setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_REMINDER)
                .build()
            val manager = context.getSystemService(NotificationManager::class.java) ?: return false
            manager.notify(id, notification)
            true
        } catch (_: Throwable) {
            false
        }
    }

    private fun createChannel(context: Context, id: String, name: String) {
        try {
            val manager = context.getSystemService(NotificationManager::class.java) ?: return
            manager.createNotificationChannel(
                NotificationChannel(id, name, NotificationManager.IMPORTANCE_HIGH)
            )
        } catch (_: Throwable) {
            // OEM ROM 的通知设置异常不应影响任务主流程。
        }
    }
}
