package com.yikou.lightfood

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.browser.customtabs.CustomTabsIntent
import java.io.File

/**
 * 监听 kdocs-cli 写入的授权 URL 文件，并拉起系统浏览器。
 *
 * 用轮询而不是 FileObserver：后者在 app-private 目录 / 低端 ROM 上偶发丢事件，
 * 轮询 300ms 对用户动作延迟无感，也更容易测试与排障。
 */
class AuthCoordinator(
    private val context: Context,
    private val urlFile: File,
    private val onUrl: (String) -> Unit,
) {
    private val handler = Handler(Looper.getMainLooper())

    @Volatile
    private var running = false

    @Volatile
    private var launchedUrl = ""

    private val poll = object : Runnable {
        override fun run() {
            if (!running) return
            pickUrl()?.let { url ->
                if (url != launchedUrl && WpsRuntimeInternals.isSupportedAuthUrl(url)) {
                    launchedUrl = url
                    onUrl(url)
                    launch(url)
                }
            }
            if (running) handler.postDelayed(this, POLL_INTERVAL_MS)
        }
    }

    fun start() {
        if (running) return
        running = true
        handler.post(poll)
    }

    fun stop() {
        running = false
        handler.removeCallbacks(poll)
    }

    private fun pickUrl(): String? {
        return try {
            if (!urlFile.isFile) return null
            val lines = urlFile.readLines(Charsets.UTF_8)
            // CLI 可能被重试，取最后一行是最新一次请求。
            lines.lastOrNull { it.isNotBlank() }?.trim()
        } catch (_: Throwable) {
            null
        }
    }

    private fun launch(url: String) {
        val uri = Uri.parse(url)
        try {
            val intent = CustomTabsIntent.Builder()
                .setShowTitle(true)
                .build()
                .intent
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent.apply { data = uri })
        } catch (_: Throwable) {
            try {
                context.startActivity(
                    Intent(Intent.ACTION_VIEW, uri)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            } catch (_: Throwable) {
                // 没有浏览器时留在应用内，前端会提示授权超时。
            }
        }
    }

    companion object {
        private const val POLL_INTERVAL_MS = 300L
    }
}
