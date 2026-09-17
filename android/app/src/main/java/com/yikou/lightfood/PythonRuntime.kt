package com.yikou.lightfood

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream

data class LocalServerInfo(
    val url: String,
    val port: Int,
    val token: String,
    val wpsOk: Boolean = true,
    val wpsMessage: String = "",
)

/**
 * Chaquopy 生命周期管理：启动解释器、抽取前端产物、调用 android_bootstrap。
 *
 * Python 只信任 Kotlin 传入的 filesDir/distDir；WpsRuntime 的 nativeLibraryDir
 * 每次初始化重新读取，因为 App 覆盖安装后目录会变。
 */
class PythonRuntime(private val context: Context) {

    @Synchronized
    fun start(): LocalServerInfo {
        // 即使 WpsRuntime 组件缺失也继续启动 Web UI；MainActivity 会弹出原生提示，
        // 满足设计约定「Android 初始化失败不能白屏」。
        val wpsStatus = WpsRuntime.initialize(context)
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context))
        }
        val distDir = extractAssetDirectory("dist", File(context.filesDir, "dist"))
        if (!File(distDir, "index.html").isFile) {
            throw IllegalStateException(
                "APK 内缺少前端产物 dist/index.html；构建前请先执行 pnpm build")
        }
        val module = Python.getInstance().getModule("android_bootstrap")
        val configureRaw = module.callAttr(
            "configure",
            context.filesDir.absolutePath,
            distDir.absolutePath,
            context.cacheDir.absolutePath,
        ).toString()
        val configure = JSONObject(configureRaw)
        if (!configure.optBoolean("ok", false)) {
            throw IllegalStateException("Python 环境初始化失败：$configureRaw")
        }

        val serverRaw = module.callAttr("start_http_server", 0).toString()
        val server = JSONObject(serverRaw)
        if (!server.optBoolean("ok", false)) {
            throw IllegalStateException("本地 HTTP 服务启动失败：$serverRaw")
        }
        return LocalServerInfo(
            url = server.optString("url"),
            port = server.optInt("port"),
            token = server.optString("token"),
            wpsOk = wpsStatus.ok,
            wpsMessage = if (wpsStatus.ok) "" else
                "${wpsStatus.errorCode ?: "RUNTIME_UNAVAILABLE"}：${wpsStatus.message}",
        )
    }

    fun diagnostics(): String {
        return try {
            if (!Python.isStarted()) {
                JSONObject(WpsRuntime.diagnosticsMap()).toString()
            } else {
                Python.getInstance()
                    .getModule("android_bootstrap")
                    .callAttr("diagnostics")
                    .toString()
            }
        } catch (exc: Throwable) {
            JSONObject(
                mapOf(
                    "ok" to false,
                    "error" to "${exc.javaClass.simpleName}: ${exc.message}",
                    "native" to WpsRuntime.diagnosticsMap(),
                )
            ).toString()
        }
    }

    private fun extractAssetDirectory(assetPath: String, dest: File): File {
        dest.mkdirs()
        val children = context.assets.list(assetPath) ?: emptyArray()
        if (children.isEmpty()) {
            copyAssetFile(assetPath, dest)
        } else {
            for (child in children) {
                val childDest = File(dest, child)
                val childAsset = "$assetPath/$child"
                val grandChildren = context.assets.list(childAsset) ?: emptyArray()
                if (grandChildren.isEmpty()) {
                    childDest.parentFile?.mkdirs()
                    copyAssetFile(childAsset, childDest)
                } else {
                    extractAssetDirectory(childAsset, childDest)
                }
            }
        }
        return dest
    }

    private fun copyAssetFile(assetPath: String, dest: File) {
        context.assets.open(assetPath).use { input ->
            dest.parentFile?.mkdirs()
            FileOutputStream(dest).use { output -> input.copyTo(output) }
        }
        try {
            dest.setReadable(true, true)
        } catch (_: Throwable) {
            // Android 内部存储本身受沙箱保护。
        }
    }
}
