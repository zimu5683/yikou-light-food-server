package com.yikou.lightfood

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * 管理后台的 Android WebView 运行层。
 *
 * Python requests 的 OpenSSL TLS 指纹可能被 WAF 的 http_bot_simple 拦截；这里用
 * 系统 WebView（Chromium）完成两类请求：
 * - ``loginJson``：登录，并让 CookieManager 保存 WAF cookie；
 * - ``fetchJson``：登录后的管理接口（订单列表/订单详情等）全部复用 Chromium 环境，
 *   避免 requests 再次被 WAF 403。
 *
 * Python 只拿后端原始 JSON，不接触 WebView；密码不会写入日志。
 */
object AdminWebLogin {
    private const val JS_BRIDGE = "YikouNative"
    private const val LOGIN_PATH = "/channel/login"
    private const val PAGE_PATH = "/admin/"

    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var activeWebView: WebView? = null

    @Volatile
    private var fetchWebView: WebView? = null

    @Volatile
    private var fetchReady = false

    @Volatile
    private var fetchPageLatch: CountDownLatch? = null

    @Volatile
    private var pendingFetch: ((String) -> Unit)? = null

    private val fetchLock = Any()

    @JvmStatic
    fun initialize(context: Context) {
        appContext = context.applicationContext
    }

    // ------------------------------------------------------------------
    // 登录
    // ------------------------------------------------------------------
    @SuppressLint("SetJavaScriptEnabled")
    @JvmStatic
    fun loginJson(origin: String, username: String, password: String, timeoutMs: Long): String {
        val ctx = appContext ?: return errorJson("RUNTIME_UNAVAILABLE", "WebView 登录未初始化")
        if (origin.isBlank() || !origin.startsWith("http")) {
            return errorJson("RUNTIME_CRASHED", "管理后台网址无效")
        }
        val result = AtomicReference<JSONObject?>()
        val submitted = AtomicBoolean(false)
        val latch = CountDownLatch(1)

        mainHandler.post {
            try {
                val webView = newWebView(ctx) { value ->
                    result.set(envelopeFromJs(value))
                    latch.countDown()
                }
                activeWebView = webView
                webView.webViewClient = object : WebViewClient() {
                    override fun onPageFinished(view: WebView?, url: String?) {
                        if (view == null || !submitted.compareAndSet(false, true)) return
                        val body = JSONObject()
                            .put("username", username)
                            .put("password", password)
                            .put("remember", false)
                            .toString()
                        val script = buildString {
                            append("(function(){")
                            append("fetch('").append(LOGIN_PATH).append("',{")
                            append("method:'POST',")
                            append("headers:{'Content-Type':'application/json','Accept':'application/json',")
                            append("'X-Requested-With':'XMLHttpRequest'},")
                            append("credentials:'include',")
                            append("body:").append(JSONObject.quote(body))
                            append("})")
                            append(".then(function(r){return r.text().then(function(t){")
                            append("window.").append(JS_BRIDGE)
                            append(".onLoginResult(JSON.stringify({status:r.status,body:t}));});})")
                            append(".catch(function(e){window.").append(JS_BRIDGE)
                            append(".onLoginResult(JSON.stringify({status:0,body:String(e)}));});")
                            append("})();")
                        }
                        view.evaluateJavascript(script, null)
                    }
                }
                webView.loadUrl(origin.trimEnd('/') + PAGE_PATH)
            } catch (exc: Throwable) {
                result.set(errorObject("RUNTIME_CRASHED",
                    "${exc.javaClass.simpleName}: ${exc.message}"))
                latch.countDown()
            }
        }

        val finished = try {
            latch.await(maxOf(1_000L, timeoutMs), TimeUnit.MILLISECONDS)
        } catch (_: InterruptedException) {
            false
        }
        mainHandler.post {
            try {
                activeWebView?.stopLoading()
                activeWebView?.destroy()
            } catch (_: Throwable) {
                // ignore
            } finally {
                activeWebView = null
            }
        }
        if (!finished) {
            return errorJson("TIMEOUT", "WebView 登录超时")
        }
        return (result.get() ?: errorObject(
            "RUNTIME_CRASHED", "WebView 登录未返回结果")).toString()
    }

    // ------------------------------------------------------------------
    // 登录后的管理接口：全部走同一个 Chromium WebView
    // ------------------------------------------------------------------
    @JvmStatic
    fun fetchJson(
        origin: String,
        path: String,
        method: String,
        token: String,
        uniacid: String,
        bodyJson: String?,
        timeoutMs: Long,
    ): String {
        val ctx = appContext ?: return errorJson("RUNTIME_UNAVAILABLE", "WebView 未初始化")
        if (origin.isBlank() || !path.startsWith("/")) {
            return errorJson("RUNTIME_CRASHED", "接口路径无效")
        }
        synchronized(fetchLock) {
            val webView = ensureFetchWebView(ctx, origin)
                ?: return errorJson("RUNTIME_CRASHED", "WebView 管理页面未就绪")
            val result = AtomicReference<String>()
            val latch = CountDownLatch(1)
            pendingFetch = { value ->
                result.set(value)
                latch.countDown()
            }
            try {
                mainHandler.post {
                    try {
                        val headers = JSONObject()
                        if (token.isNotBlank()) headers.put("Authorization", "Bearer $token")
                        if (uniacid.isNotBlank()) headers.put("uniacid", uniacid)
                        if (!bodyJson.isNullOrBlank()) headers.put("Content-Type", "application/json")
                        val options = JSONObject()
                            .put("method", method.ifBlank { "GET" }.uppercase())
                            .put("headers", headers)
                            .put("credentials", "include")
                        if (!bodyJson.isNullOrBlank()) options.put("body", bodyJson)
                        val script = buildString {
                            append("(function(){")
                            append("fetch(").append(JSONObject.quote(path)).append(",")
                            append(options.toString()).append(")")
                            append(".then(function(r){return r.text().then(function(t){")
                            append("window.").append(JS_BRIDGE)
                            append(".onFetchResult(JSON.stringify({status:r.status,body:t}));});})")
                            append(".catch(function(e){window.").append(JS_BRIDGE)
                            append(".onFetchResult(JSON.stringify({status:0,body:String(e)}));});")
                            append("})();")
                        }
                        webView.evaluateJavascript(script, null)
                    } catch (exc: Throwable) {
                        result.set(errorObject("RUNTIME_CRASHED",
                            "${exc.javaClass.simpleName}: ${exc.message}").toString())
                        latch.countDown()
                    }
                }
                val finished = try {
                    latch.await(maxOf(1_000L, timeoutMs), TimeUnit.MILLISECONDS)
                } catch (_: InterruptedException) {
                    false
                }
                if (!finished) return errorJson("TIMEOUT", "WebView 接口请求超时")
                return result.get() ?: errorJson("RUNTIME_CRASHED", "WebView 接口请求无结果")
            } finally {
                pendingFetch = null
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun newWebView(ctx: Context,
                          onLogin: ((String) -> Unit)? = null): WebView {
        val webView = WebView(ctx)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = false
        webView.settings.allowContentAccess = false
        webView.settings.javaScriptCanOpenWindowsAutomatically = false
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)
        webView.addJavascriptInterface(object {
            @JavascriptInterface
            fun onLoginResult(value: String) {
                onLogin?.invoke(value)
            }

            @JavascriptInterface
            fun onFetchResult(value: String) {
                pendingFetch?.invoke(value)
            }
        }, JS_BRIDGE)
        return webView
    }

    private fun ensureFetchWebView(ctx: Context, origin: String): WebView? {
        synchronized(fetchLock) {
            if (fetchWebView != null && fetchReady) return fetchWebView
            if (fetchWebView != null && !fetchReady) {
                fetchPageLatch?.await(15, TimeUnit.SECONDS)
                if (fetchReady) return fetchWebView
            }
            fetchReady = false
            val latch = CountDownLatch(1)
            fetchPageLatch = latch
            mainHandler.post {
                try {
                    val webView = newWebView(ctx)
                    fetchWebView = webView
                    webView.webViewClient = object : WebViewClient() {
                        override fun onPageFinished(view: WebView?, url: String?) {
                            fetchReady = true
                            fetchPageLatch?.countDown()
                        }
                    }
                    webView.loadUrl(origin.trimEnd('/') + PAGE_PATH)
                } catch (_: Throwable) {
                    fetchPageLatch?.countDown()
                }
            }
            latch.await(20, TimeUnit.SECONDS)
            return fetchWebView?.takeIf { fetchReady }
        }
    }

    private fun envelopeFromJs(value: String): JSONObject {
        return try {
            val parsed = JSONObject(value)
            JSONObject()
                .put("ok", true)
                .put("status", parsed.optInt("status", 0))
                .put("body", parsed.optString("body", ""))
        } catch (exc: Throwable) {
            errorObject("RUNTIME_CRASHED", "WebView 返回异常：${exc.message}")
        }
    }

    private fun errorObject(code: String, message: String): JSONObject =
        JSONObject().put("ok", false).put("errorCode", code).put("message", message)

    private fun errorJson(code: String, message: String): String =
        errorObject(code, message).toString()
}
