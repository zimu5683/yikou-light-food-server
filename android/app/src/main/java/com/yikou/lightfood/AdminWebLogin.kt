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
 * 管理后台 WebView 登录兜底。
 *
 * Python requests 的 OpenSSL TLS 指纹可能被 WAF 的 http_bot_simple 拦截；这里用
 * 系统 WebView（Chromium）先加载真实管理页拿到 WAF cookie，再在页面上下文里执行
 * 同源 fetch('/channel/login')。Python 只拿回后端原始 JSON，不接触 WebView。
 */
object AdminWebLogin {
    private const val JS_BRIDGE = "YikouNative"
    private const val LOGIN_PATH = "/channel/login"
    private const val PAGE_PATH = "/admin/"

    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var activeWebView: WebView? = null

    @JvmStatic
    fun initialize(context: Context) {
        appContext = context.applicationContext
    }

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
        val handler = Handler(Looper.getMainLooper())

        handler.post {
            try {
                val webView = WebView(ctx)
                activeWebView = webView
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
                        result.set(envelopeFromJs(value))
                        latch.countDown()
                    }
                }, JS_BRIDGE)

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
        handler.post {
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
