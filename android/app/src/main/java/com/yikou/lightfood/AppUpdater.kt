package com.yikou.lightfood

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL

data class UpdateInfo(
    val tagName: String,
    val version: String,
    val body: String,
    val htmlUrl: String,
    val apkUrl: String = "",
)

/**
 * GitHub Releases 更新：只负责检查和下载，安装交给系统 PackageInstaller。
 *
 * 正式分发必须使用同一把 release keystore，否则新 APK 无法覆盖安装；CI 从
 * YIKOU_KEYSTORE_* secrets 读取，缺失时 M0/M1 构建退回 debug 签名。
 */
object AppUpdater {
    private const val DEFAULT_REPOSITORY = "zimu5683/yikou-light-food-server"
    private const val USER_AGENT = "yikou-light-food-android"

    fun checkForUpdate(currentVersion: String,
                       repository: String = DEFAULT_REPOSITORY): UpdateInfo? {
        val payload = httpGetJson("https://api.github.com/repos/$repository/releases/latest")
            ?: return null
        val tag = payload.optString("tag_name").trim()
        if (tag.isEmpty()) return null
        val version = tag.removePrefix("v")
        if (compareVersions(version, currentVersion) <= 0) return null
        val assets = payload.optJSONArray("assets") ?: return null
        var apkUrl = ""
        for (index in 0 until assets.length()) {
            val asset = assets.optJSONObject(index) ?: continue
            val name = asset.optString("name").lowercase()
            if (!name.endsWith(".apk")) continue
            val url = asset.optString("browser_download_url")
            // 优先明确的 arm64 包；没有则退回第一个 APK，便于手工下载通用包。
            if (name.contains("arm64") || name.contains("universal")) {
                apkUrl = url
                break
            }
            if (apkUrl.isEmpty()) apkUrl = url
        }
        // 没有 APK 资产的 Release 不是 Android 发布包（可能是旧的网页端/桌面端），
        // 不弹「发现新版本」，避免用户点了却下载不到可安装文件。
        if (apkUrl.isBlank()) return null
        return UpdateInfo(
            tagName = tag,
            version = version,
            body = payload.optString("body"),
            htmlUrl = payload.optString("html_url"),
            apkUrl = apkUrl,
        )
    }

    fun downloadAndInstall(context: Context, info: UpdateInfo) {
        if (info.apkUrl.isBlank()) {
            openExternal(context, info.htmlUrl)
            return
        }
        Thread {
            try {
                val directory = File(context.cacheDir, "updates").apply { mkdirs() }
                val apk = File(directory, "yikou-${info.version}.apk")
                val url = URL(info.apkUrl)
                val connection = (url.openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15_000
                    readTimeout = 60_000
                    setRequestProperty("User-Agent", USER_AGENT)
                }
                connection.inputStream.use { input ->
                    FileOutputStream(apk).use { output -> input.copyTo(output) }
                }
                install(context, apk)
            } catch (_: Throwable) {
                openExternal(context, info.htmlUrl)
            }
        }.start()
    }

    private fun install(context: Context, apk: File) {
        val uri: Uri = FileProvider.getUriForFile(
            context, "${context.packageName}.fileprovider", apk)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }

    private fun openExternal(context: Context, url: String) {
        if (url.isBlank()) return
        try {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: Throwable) {
            // 没有浏览器时静默失败；更新只是提示，不应打断主流程。
        }
    }

    /** 语义化版本比较，兼容 ``v3.5.0`` 与 ``3.5.0``；不可比时返回 -1。 */
    fun compareVersions(left: String, right: String): Int {
        val lhs = parseVersion(left) ?: return -1
        val rhs = parseVersion(right) ?: return -1
        for (index in 0 until maxOf(lhs.size, rhs.size)) {
            val a = lhs.getOrElse(index) { 0 }
            val b = rhs.getOrElse(index) { 0 }
            if (a != b) return if (a > b) 1 else -1
        }
        return 0
    }

    private fun parseVersion(value: String): List<Int>? {
        val parts = value.removePrefix("v").split(".")
        val numbers = parts.map { it.toIntOrNull() ?: return null }
        return if (numbers.size == 3) numbers else null
    }

    private fun httpGetJson(url: String): JSONObject? {
        return try {
            val connection = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = 10_000
                readTimeout = 10_000
                setRequestProperty("Accept", "application/vnd.github+json")
                setRequestProperty("User-Agent", USER_AGENT)
            }
            if (connection.responseCode != HttpURLConnection.HTTP_OK) return null
            val body = connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            JSONObject(body)
        } catch (_: Throwable) {
            null
        }
    }
}
