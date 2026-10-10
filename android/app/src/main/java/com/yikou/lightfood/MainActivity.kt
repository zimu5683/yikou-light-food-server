package com.yikou.lightfood

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File

/**
 * 承载现有 React 前端的 WebView。所有业务功能仍走 127.0.0.1 上的 Bridge API；
 * 原生层只负责权限引导、启动前台 Service、外链和失败兜底诊断。
 */
class MainActivity : Activity() {
    private lateinit var webView: WebView
    private lateinit var statusView: TextView
    private lateinit var root: FrameLayout

    private var loadedUrl = ""
    private var lastWarning = ""
    private var pendingExportFile: File? = null

    private val stateListener: (RuntimeSnapshot) -> Unit = { snapshot ->
        runOnUiThread { render(snapshot) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        RuntimeState.addListener(stateListener)
        TaskService.start(this)
        requestStartupPermissions()
    }

    override fun onDestroy() {
        RuntimeState.removeListener(stateListener)
        super.onDestroy()
    }

    @Deprecated("平台返回键逻辑足够简单；WebView 内先返回，再退出页面。")
    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")  // 本地 127.0.0.1 页面且只加载自有前端。
    private fun buildUi() {
        root = FrameLayout(this).apply { setBackgroundColor(Color.rgb(247, 243, 234)) }
        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.cacheMode = WebSettings.LOAD_DEFAULT
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            visibility = View.INVISIBLE
            webChromeClient = WebChromeClient()
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(
                    view: WebView,
                    request: WebResourceRequest,
                ): Boolean = handleUrl(request.url)

                @Suppress("DEPRECATION")
                override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean =
                    handleUrl(Uri.parse(url))
            }
            setBackgroundColor(Color.rgb(247, 243, 234))
        }
        installExportBridge()
        root.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )

        statusView = TextView(this).apply {
            text = "正在启动本地服务…"
            textSize = 15f
            setTextColor(Color.rgb(46, 42, 37))
            setPadding(48, 48, 48, 48)
            gravity = android.view.Gravity.CENTER
            setOnClickListener {
                Toast.makeText(this@MainActivity, "正在重启服务…", Toast.LENGTH_SHORT).show()
                stopService(Intent(this@MainActivity, TaskService::class.java))
                TaskService.start(this@MainActivity)
            }
        }
        root.addView(
            statusView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        setContentView(root)
    }

    private fun handleUrl(uri: Uri): Boolean {
        val host = uri.host ?: return true
        val isLocal = uri.scheme.equals("http", true) &&
            (host == "127.0.0.1" || host.equals("localhost", true))
        if (isLocal) return false
        val opened = WpsRuntime.openExternal(uri.toString())
        if (!opened) {
            Toast.makeText(this, "没有可打开该链接的浏览器", Toast.LENGTH_SHORT).show()
        }
        return true
    }

    // ------------------------------------------------------------------
    // 闪时送诊断日志导出：系统「另存为」（可自选文件夹）与分享
    //
    // WebView 不会自己保存网页发起的下载（blob + a[download] 在手机上静默失效），
    // 因此前端通过 ``window.YikouExport`` 调到这里：保存走 SAF 的
    // ACTION_CREATE_DOCUMENT（用户自己浏览/选择任意文件夹、可改文件名，无需任何
    // 存储权限），分享走 FileProvider + ACTION_SEND（可直接发微信）。
    // ------------------------------------------------------------------
    private fun installExportBridge() {
        webView.addJavascriptInterface(object {
            @JavascriptInterface
            fun saveDiagnostics(name: String?) {
                val file = resolveDiagnosticFile(name)
                if (file == null) {
                    notifyExport("failed", "", "文件名不合法或文件不存在")
                    return
                }
                runOnUiThread { startDiagnosticSave(file) }
            }

            @JavascriptInterface
            fun shareDiagnostics(name: String?) {
                val file = resolveDiagnosticFile(name)
                if (file == null) {
                    notifyExport("failed", "", "文件名不合法或文件不存在")
                    return
                }
                runOnUiThread { startDiagnosticShare(file) }
            }
        }, EXPORT_BRIDGE)
    }

    /** 解析并校验导出的诊断文件：只允许 ``sss-diagnostics`` 目录内的 .jsonl。 */
    private fun resolveDiagnosticFile(name: String?): File? {
        val raw = name?.trim().orEmpty()
        if (!EXPORT_FILE_RE.matches(raw)) return null
        return try {
            val directory = File(filesDir, DIAGNOSTICS_SUBDIR).canonicalFile
            val canonical = File(directory, raw).canonicalFile
            if (canonical.parentFile == directory && canonical.isFile) canonical else null
        } catch (_: Throwable) {
            null
        }
    }

    @Suppress("DEPRECATION")
    private fun startDiagnosticSave(file: File) {
        pendingExportFile = file
        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = EXPORT_MIME
            putExtra(Intent.EXTRA_TITLE, "sss-diagnostics-${file.name}")
        }
        try {
            startActivityForResult(intent, RC_EXPORT_SAVE)
        } catch (_: Throwable) {
            pendingExportFile = null
            notifyExport("failed", file.name, "系统没有可用的保存对话框")
        }
    }

    private fun startDiagnosticShare(file: File) {
        try {
            val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = EXPORT_MIME
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(Intent.createChooser(intent, "分享诊断日志"))
            notifyExport("shared", file.name, "")
        } catch (_: Throwable) {
            notifyExport("failed", file.name, "没有可用的分享目标")
        }
    }

    @Deprecated("平台自带的保存结果回调；本项目仍是普通 Activity，够用且无额外依赖。")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != RC_EXPORT_SAVE) return
        val file = pendingExportFile
        pendingExportFile = null
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null || file == null) {
            notifyExport("cancelled", file?.name.orEmpty(), "")
            return
        }
        try {
            val stream = contentResolver.openOutputStream(uri, "wt")
                ?: throw IllegalStateException("无法写入所选位置")
            stream.use { output -> file.inputStream().use { input -> input.copyTo(output) } }
            notifyExport("saved", displayNameOf(uri) ?: file.name, "")
        } catch (exc: Throwable) {
            notifyExport("failed", file.name, exc.message ?: "写入所选位置失败")
        }
    }

    /** SAF 返回的 document uri 末段形如 ``primary:Download/xx.jsonl``，解出可读展示名。 */
    private fun displayNameOf(uri: Uri): String? {
        val segment = uri.lastPathSegment ?: return null
        val decoded = Uri.decode(segment)
        return decoded.substringAfter(':', decoded).ifBlank { null }
    }

    private fun notifyExport(state: String, name: String, error: String) {
        runOnUiThread {
            val text = when (state) {
                "saved" -> "已保存：$name"
                "shared" -> "已打开分享面板"
                "cancelled" -> "已取消保存"
                else -> "导出失败：$error"
            }
            Toast.makeText(this, text, Toast.LENGTH_LONG).show()
            if (this::webView.isInitialized) {
                val payload = JSONObject()
                    .put("state", state)
                    .put("name", name)
                    .put("error", error)
                    .toString()
                try {
                    webView.evaluateJavascript(
                        "window.__yikouExportResult && window.__yikouExportResult($payload)",
                        null,
                    )
                } catch (_: Throwable) {
                    // 页面已销毁时只保留原生提示。
                }
            }
        }
    }

    private fun render(snapshot: RuntimeSnapshot) {
        when (snapshot.state) {
            "server_ready" -> {
                statusView.visibility = View.GONE
                webView.visibility = View.VISIBLE
                if (snapshot.warning.isNotBlank() && snapshot.warning != lastWarning) {
                    lastWarning = snapshot.warning
                    Toast.makeText(this, snapshot.warning, Toast.LENGTH_LONG).show()
                }
                if (snapshot.serverUrl.isNotBlank() && snapshot.serverUrl != loadedUrl) {
                    loadedUrl = snapshot.serverUrl
                    webView.loadUrl(snapshot.serverUrl)
                }
            }
            "error" -> {
                webView.visibility = View.INVISIBLE
                statusView.visibility = View.VISIBLE
                statusView.text = buildString {
                    append("启动失败，点击屏幕重试\n\n")
                    append(snapshot.message)
                    if (snapshot.diagnostics.isNotBlank()) {
                        append("\n\n诊断：")
                        append(snapshot.diagnostics.take(2000))
                    }
                }
            }
            else -> {
                webView.visibility = View.INVISIBLE
                statusView.visibility = View.VISIBLE
                statusView.text = "正在启动本地服务…"
            }
        }
    }

    // ------------------------------------------------------------------
    // 权限引导
    // ------------------------------------------------------------------
    private fun requestStartupPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), RC_NOTIFICATIONS)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                // 先处理存储；电池优化弹窗等存储弹窗关闭后再出，避免叠两层 Dialog。
                showStorageDialog()
                return
            }
        } else if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                arrayOf(
                    Manifest.permission.READ_EXTERNAL_STORAGE,
                    Manifest.permission.WRITE_EXTERNAL_STORAGE,
                ),
                RC_STORAGE,
            )
            return
        }
        maybeAskBatteryOptimization()
    }

    private fun showStorageDialog() {
        AlertDialog.Builder(this)
            .setTitle("需要文件访问权限")
            .setMessage(
                "选择/保存 Excel 排单表需要读取手机存储。" +
                    "请授予「所有文件访问权限」，任务不会上传你的文件。"
            )
            .setPositiveButton("去授权") { _, _ ->
                openAllFilesAccessSettings()
                maybeAskBatteryOptimization()
            }
            .setNegativeButton("暂不") { _, _ -> maybeAskBatteryOptimization() }
            .show()
    }

    private fun openAllFilesAccessSettings() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        val appUri = Uri.parse("package:$packageName")
        try {
            startActivity(
                Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, appUri)
            )
        } catch (_: Throwable) {
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            } catch (_: Throwable) {
                Toast.makeText(this, "请在系统设置里手动授权", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun maybeAskBatteryOptimization() {
        val prefs = getSharedPreferences("yikou_startup", Context.MODE_PRIVATE)
        if (prefs.getBoolean("battery_prompted", false)) return
        AlertDialog.Builder(this)
            .setTitle("保持后台运行")
            .setMessage("为防止息屏后任务被系统中断，建议将本应用加入电池优化白名单。")
            .setPositiveButton("去设置") { _, _ ->
                try {
                    startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                } catch (_: Throwable) {
                    // 部分 ROM 没有标准入口。
                }
            }
            .setNegativeButton("以后") { _, _ -> }
            .show()
        prefs.edit().putBoolean("battery_prompted", true).apply()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        // 拒绝通知权限只影响前台 Service 通知显示；任务照常运行。
        if (requestCode == RC_STORAGE) {
            maybeAskBatteryOptimization()
        }
    }

    companion object {
        private const val RC_NOTIFICATIONS = 1001
        private const val RC_STORAGE = 1002
        private const val RC_EXPORT_SAVE = 1003

        private const val EXPORT_BRIDGE = "YikouExport"
        private const val EXPORT_MIME = "application/octet-stream"
        private const val DIAGNOSTICS_SUBDIR = "config/sss-diagnostics"
        private val EXPORT_FILE_RE = Regex("^[A-Za-z0-9._-]+\\.jsonl$")
    }
}
