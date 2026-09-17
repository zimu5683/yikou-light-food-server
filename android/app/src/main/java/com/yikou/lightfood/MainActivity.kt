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
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast

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

    private val stateListener: (RuntimeSnapshot) -> Unit = { snapshot ->
        runOnUiThread { render(snapshot) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        RuntimeState.addListener(stateListener)
        TaskService.start(this)
        requestStartupPermissions()
        checkForUpdates()
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

    private fun checkForUpdates() {
        // GitHub 检查失败绝不打扰启动；有更新时才弹原生对话框，下载后交给
        // PackageInstaller，不复用网页版的 update:available 事件。
        Thread {
            val update = try {
                AppUpdater.checkForUpdate(BuildConfig.VERSION_NAME)
            } catch (_: Throwable) {
                null
            }
            if (update != null) {
                runOnUiThread { showUpdateDialog(update) }
            }
        }.start()
    }

    private fun showUpdateDialog(info: UpdateInfo) {
        val message = buildString {
            append("当前版本：").append(BuildConfig.VERSION_NAME).append("\n")
            append("最新版本：").append(info.tagName).append("\n\n")
            append(info.body.take(1200))
        }
        AlertDialog.Builder(this)
            .setTitle("发现新版本")
            .setMessage(message)
            .setPositiveButton("下载并安装") { _, _ ->
                AppUpdater.downloadAndInstall(this, info)
                Toast.makeText(this, "开始下载更新…", Toast.LENGTH_SHORT).show()
            }
            .setNeutralButton("查看发布页") { _, _ ->
                if (info.htmlUrl.isNotBlank()) WpsRuntime.openExternal(info.htmlUrl)
            }
            .setNegativeButton("稍后") { _, _ -> }
            .show()
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
    }
}
