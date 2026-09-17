package com.yikou.lightfood

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.Uri
import android.os.Build
import android.util.Base64
import androidx.browser.customtabs.CustomTabsIntent
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

data class RuntimeStatus(
    val ok: Boolean,
    val errorCode: String? = null,
    val message: String = "",
    val nativeLibraryDir: String = "",
    val details: Map<String, String> = emptyMap(),
)

data class ExecResult(
    val exitCode: Int = -1,
    val stdout: String = "",
    val stderr: String = "",
    val timedOut: Boolean = false,
    val errorCode: String? = null,
)

data class AuthStatus(
    val authenticated: Boolean = false,
    val errorCode: String? = null,
    val message: String = "",
)

data class AuthResult(
    val ok: Boolean = false,
    val url: String = "",
    val errorCode: String? = null,
    val message: String = "",
)

private data class AuthState(
    val running: Boolean = false,
    val finished: Boolean = false,
    val ok: Boolean = false,
    val authenticated: Boolean = false,
    val url: String = "",
    val errorCode: String? = null,
    val message: String = "",
)

/**
 * kdocs-cli 的 Android 兼容层：proot + 固定版本 CLI + DNS/CA/假浏览器。
 *
 * 对外主接口见 design/APK-PLAN.md §2.2；额外提供若干 ``*Json`` @JvmStatic 方法，
 * 因为 Chaquopy 直接传 Kotlin data class/函数类型不可靠。Python 只调用这些
 * 扁平入口，接口稳定后不再改名字。
 */
object WpsRuntime {
    private const val WPS_DIR = "wps"
    private const val LOGIN_TIMEOUT_MS = 660_000L
    private const val AUTH_URL_FILE = "auth_url.txt"
    private const val PROOT_LOADER = "libproot_loader.so"

    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var nativeLibraryDir: String = ""

    @Volatile
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    private val lock = Any()
    private val dnsLock = Any()

    @Volatile
    private var authState = AuthState()

    @SuppressLint("StaticFieldLeak")  // 只持有 applicationContext，生命周期与进程一致。
    @Volatile
    private var authCoordinator: AuthCoordinator? = null

    @Volatile
    private var authThread: Thread? = null

    @Volatile
    private var authProcess: Process? = null

    // ------------------------------------------------------------------
    // 初始化 / DNS
    // ------------------------------------------------------------------
    @JvmStatic
    fun initialize(context: Context): RuntimeStatus {
        synchronized(lock) {
            val ctx = context.applicationContext
            appContext = ctx
            nativeLibraryDir = ctx.applicationInfo.nativeLibraryDir

            val wpsDir = wpsDir(ctx)
            for (directory in listOf(wpsDir, File(wpsDir, "tmp"), File(wpsDir, "home"),
                File(wpsDir, "home/.config"))) {
                directory.mkdirs()
            }
            copyCaBundle(ctx)
            refreshResolvConf(ctx)
            registerNetworkCallback(ctx)
            InteractionNotifier.initialize(ctx)

            val missing = runtimeBinaryFiles(ctx).filter { !it.isFile }.map { it.name }
            if (missing.isNotEmpty()) {
                return RuntimeStatus(
                    ok = false,
                    errorCode = "PROOT_MISSING",
                    message = "内嵌运行时缺少组件：" + missing.joinToString("、"),
                    nativeLibraryDir = nativeLibraryDir,
                )
            }
            if (!File(wpsDir, "cacert.pem").isFile) {
                return RuntimeStatus(
                    ok = false,
                    errorCode = "TLS_CA_FAILED",
                    message = "CA bundle 缺失；请运行 scripts/fetch_android_runtime.py 重新打包",
                    nativeLibraryDir = nativeLibraryDir,
                )
            }
            return RuntimeStatus(
                ok = true,
                message = "WpsRuntime 已就绪",
                nativeLibraryDir = nativeLibraryDir,
                details = mapOf(
                    "targetSdk" to Build.VERSION.SDK_INT.toString(),
                    "dns" to readDnsServers(ctx).joinToString(","),
                ),
            )
        }
    }

    @JvmStatic
    fun initializationStatusJson(): String =
        toJson(initializeStatusMap())

    @JvmStatic
    fun diagnosticsJson(): String = JSONObject(diagnosticsMap()).toString()

    private fun initializeStatusMap(): Map<String, Any?> {
        val ctx = appContext
        return if (ctx == null) {
            mapOf("ok" to false, "errorCode" to "RUNTIME_UNAVAILABLE",
                "message" to "WpsRuntime.initialize() 尚未调用")
        } else {
            val status = initialize(ctx)
            mapOf(
                "ok" to status.ok,
                "errorCode" to status.errorCode,
                "message" to status.message,
                "nativeLibraryDir" to status.nativeLibraryDir,
            )
        }
    }

    /** 打包进 APK 的 nativeLibraryDir（只读、可执行）。 */
    private fun nativeLibraryDir(context: Context): File =
        File(context.applicationInfo.nativeLibraryDir)

    /** 实际执行目录：默认 nativeLibraryDir；targetSdk<=28 时复制到 filesDir 下执行。 */
    private fun effectiveBinaryDir(context: Context): File {
        val nativeDir = nativeLibraryDir(context)
        return if (WpsRuntimeInternals.shouldUseFilesDirFallback(
                context.applicationInfo.targetSdkVersion)) {
            File(wpsDir(context), "runtime")
        } else {
            nativeDir
        }
    }

    /**
     * 返回实际执行路径下的 6 个组件。
     *
     * 默认 ``targetSdk 35`` 直接使用 nativeLibraryDir；只有用户按 M0 停机点降级到
     * targetSdk 28 时，才从 nativeLibraryDir 复制到 ``filesDir/wps/runtime`` 并补上
     * 可执行位，绕过 Android 10+ 的 W^X 限制。
     */
    private fun runtimeBinaryFiles(context: Context): List<File> {
        val nativeDir = nativeLibraryDir(context)
        val targetDir = effectiveBinaryDir(context)
        if (targetDir.canonicalPath != nativeDir.canonicalPath) {
            targetDir.mkdirs()
            for (name in WpsRuntimeInternals.RUNTIME_BINARY_NAMES) {
                val source = File(nativeDir, name)
                val dest = File(targetDir, name)
                if (!source.isFile) continue
                if (!dest.isFile || dest.length() != source.length()) {
                    try {
                        source.copyTo(dest, overwrite = true)
                        dest.setReadable(true, false)
                        dest.setExecutable(true, false)
                    } catch (_: Throwable) {
                        // 检查阶段会把缺失/不可用文件报成 PROOT_MISSING。
                    }
                }
            }
        }
        return WpsRuntimeInternals.RUNTIME_BINARY_NAMES.map { File(targetDir, it) }
    }

    private fun wpsDir(context: Context): File = File(context.filesDir, WPS_DIR)

    private fun copyCaBundle(context: Context): Boolean {
        val dest = File(wpsDir(context), "cacert.pem")
        return try {
            val bytes = context.assets.open("runtime/cacert.pem").use { it.readBytes() }
            val tmp = File(dest.parentFile, "${dest.name}.tmp")
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(dest)) {
                dest.writeBytes(bytes)
                tmp.delete()
            }
            dest.isFile
        } catch (_: Throwable) {
            false
        }
    }

    @Suppress("DEPRECATION")  // getAllNetworks/getLinkProperties 在 API 31+ 仍要兼容旧 ROM/VPN。
    private fun readDnsServers(context: Context): List<String> {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return WpsRuntimeInternals.DEFAULT_DNS
        val discovered = mutableListOf<String>()
        try {
            for (network in manager.allNetworks) {
                val properties = manager.getLinkProperties(network) ?: continue
                for (address in properties.dnsServers) {
                    address.hostAddress?.let { discovered.add(it) }
                }
            }
        } catch (_: Throwable) {
            // 系统 API / OEM ROM 异常时走 fallback。
        }
        return WpsRuntimeInternals.normalizeDnsServers(discovered)
    }

    private fun refreshResolvConf(context: Context) {
        synchronized(dnsLock) {
            val dns = readDnsServers(context)
            if (dns.isEmpty()) return
            val target = File(wpsDir(context), "resolv.conf")
            try {
                val text = WpsRuntimeInternals.composeResolvConf(dns)
                val tmp = File(target.parentFile, "${target.name}.tmp")
                tmp.writeText(text, Charsets.UTF_8)
                if (!tmp.renameTo(target)) {
                    target.writeText(text, Charsets.UTF_8)
                    tmp.delete()
                }
            } catch (_: Throwable) {
                // 后续 run 时会返回 DNS_CONFIG_EMPTY / DNS_FAILED 诊断。
            }
        }
    }

    private fun registerNetworkCallback(context: Context) {
        if (networkCallback != null) return
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                refreshResolvConf(context)
            }

            override fun onLost(network: Network) {
                refreshResolvConf(context)
            }

            override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
                refreshResolvConf(context)
            }
        }
        try {
            manager.registerDefaultNetworkCallback(callback)
            networkCallback = callback
        } catch (_: Throwable) {
            // 缺少 ACCESS_NETWORK_STATE 时忽略；网络切换后由下一次 run 前刷新兜底。
        }
    }

    // ------------------------------------------------------------------
    // 命令执行
    // ------------------------------------------------------------------
    @JvmStatic
    fun run(args: List<String>, paramsJson: String?, timeoutMs: Long): ExecResult =
        runCommand(args, paramsJson, timeoutMs, onProcess = null)

    @JvmStatic
    fun runJson(argsJson: String, paramsJson: String?, timeoutMs: Long): String {
        val args = try {
            val array = JSONArray(argsJson)
            (0 until array.length()).map { array.getString(it) }
        } catch (exc: Throwable) {
            return toJson(ExecResult(exitCode = -1, stderr = "非法命令参数：${exc.message}",
                errorCode = "RUNTIME_CRASHED"))
        }
        return toJson(run(args, paramsJson, timeoutMs))
    }

    private fun runCommand(
        args: List<String>,
        paramsJson: String?,
        timeoutMs: Long,
        onProcess: ((Process) -> Unit)?,
    ): ExecResult {
        val ctx = appContext ?: return ExecResult(
            stderr = "Android 运行时未初始化",
            errorCode = "RUNTIME_UNAVAILABLE",
        )
        val expected = runtimeBinaryFiles(ctx)
        val missing = expected.filter { !it.isFile }
        if (missing.isNotEmpty()) {
            return ExecResult(
                stderr = "运行时组件缺失：" + missing.joinToString("、") { it.name },
                errorCode = "PROOT_MISSING",
            )
        }
        val nativeDir = expected.first().parentFile ?: File(nativeLibraryDir)
        val proot = File(nativeDir, "libproot.so")
        val kdocs = File(nativeDir, "libkdocs_cli.so")
        val shim = File(nativeDir, "libxdgopen_shim.so")
        val wpsDir = wpsDir(ctx)
        val tmpDir = File(wpsDir, "tmp").apply { mkdirs() }
        val resolvConf = File(wpsDir, "resolv.conf")

        if (!resolvConf.isFile || resolvConf.length() == 0L) {
            refreshResolvConf(ctx)
        }
        if (!resolvConf.isFile || resolvConf.readText().isBlank()) {
            return ExecResult(stderr = "未获取到 DNS 配置", errorCode = "DNS_CONFIG_EMPTY")
        }

        val paramFile = paramsJson?.let {
            File(tmpDir, "params-${UUID.randomUUID()}.json").apply {
                writeText(it, Charsets.UTF_8)
            }
        }
        val authUrlFile = File(wpsDir, AUTH_URL_FILE)

        val command = WpsRuntimeInternals.buildCommand(
            prootPath = proot.absolutePath,
            kdocsPath = kdocs.absolutePath,
            shimPath = shim.absolutePath,
            resolvConfPath = resolvConf.absolutePath,
            args = args,
            paramsFile = paramFile?.absolutePath,
        )

        val builder = ProcessBuilder(command)
        builder.directory(File(wpsDir, "home"))
        val env = builder.environment()
        env["HOME"] = File(wpsDir, "home").absolutePath
        env["XDG_CONFIG_HOME"] = File(wpsDir, "home/.config").absolutePath
        env["SSL_CERT_FILE"] = File(wpsDir, "cacert.pem").absolutePath
        env["YIKOU_AUTH_URL_FILE"] = authUrlFile.absolutePath
        env["PROOT_TMP_DIR"] = tmpDir.absolutePath
        env["TMPDIR"] = tmpDir.absolutePath
        env["PROOT_LOADER"] = File(nativeDir, PROOT_LOADER).absolutePath
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        env["PATH"] = WpsRuntimeInternals.RUNTIME_PATH

        var processToCleanup: Process? = null
        try {
            val started = builder.start()
            processToCleanup = started
            onProcess?.invoke(started)
            val stdoutBuffer = ByteArrayOutputStream()
            val stderrBuffer = ByteArrayOutputStream()
            val outThread = Thread { copyStream(started.inputStream, stdoutBuffer) }
            val errThread = Thread { copyStream(started.errorStream, stderrBuffer) }
            outThread.start()
            errThread.start()

            var timedOut = false
            if (!started.waitFor(timeoutMs, TimeUnit.MILLISECONDS)) {
                timedOut = true
                started.destroyForcibly()
                started.waitFor(2, TimeUnit.SECONDS)
            }
            outThread.join(1_000)
            errThread.join(1_000)
            val exitCode = try {
                started.exitValue()
            } catch (_: Throwable) {
                -1
            }
            val stdout = stdoutBuffer.toString(Charsets.UTF_8.name())
            val stderr = stderrBuffer.toString(Charsets.UTF_8.name())
            return ExecResult(
                exitCode = exitCode,
                stdout = stdout,
                stderr = stderr,
                timedOut = timedOut,
                errorCode = classifyError(exitCode, stdout, stderr, timedOut),
            )
        } catch (exc: Throwable) {
            return ExecResult(
                exitCode = -1,
                stderr = "${exc.javaClass.simpleName}: ${exc.message}",
                errorCode = classifyError(-1, "", exc.message ?: "", false)
                    ?: "RUNTIME_CRASHED",
            )
        } finally {
            try {
                processToCleanup?.destroy()
            } catch (_: Throwable) {
                // ignore
            }
            paramFile?.delete()
        }
    }

    private fun copyStream(input: java.io.InputStream, output: ByteArrayOutputStream) {
        try {
            input.use { stream ->
                val buffer = ByteArray(8192)
                while (true) {
                    val read = stream.read(buffer)
                    if (read < 0) break
                    output.write(buffer, 0, read)
                }
            }
        } catch (_: Throwable) {
            // 进程被杀时读到半截属正常，保留已有内容。
        }
    }

    private fun classifyError(
        exitCode: Int,
        stdout: String,
        stderr: String,
        timedOut: Boolean,
    ): String? = WpsRuntimeInternals.classifyCliFailure(exitCode, stdout, stderr, timedOut)

    // ------------------------------------------------------------------
    // 认证
    // ------------------------------------------------------------------
    @JvmStatic
    fun authStatus(): AuthStatus {
        if (appContext == null) {
            return AuthStatus(errorCode = "RUNTIME_UNAVAILABLE", message = "未初始化")
        }
        val result = runCommand(listOf("auth", "status"), null, 60_000, onProcess = null)
        if (result.errorCode != null && result.stdout.isBlank()) {
            return AuthStatus(false, result.errorCode,
                result.stderr.ifBlank { "auth status 执行失败" })
        }
        return try {
            val json = JSONObject(result.stdout.trim())
            AuthStatus(
                authenticated = json.optBoolean("authenticated", false),
                errorCode = result.errorCode,
                message = result.stderr.take(200),
            )
        } catch (exc: Throwable) {
            AuthStatus(
                authenticated = false,
                errorCode = result.errorCode ?: "AUTH_STATUS_BAD_JSON",
                message = "auth status 输出无法解析：${exc.message}",
            )
        }
    }

    @JvmStatic
    fun authStatusJson(): String = toJson(authStatus())

    @JvmStatic
    fun beginAuthorizeJson(): String = toJson(beginAuth(null))

    @JvmStatic
    fun authorizationStateJson(): String = JSONObject(mapOf(
        "running" to authState.running,
        "finished" to authState.finished,
        "ok" to authState.ok,
        "authenticated" to authState.authenticated,
        "url" to authState.url,
        "errorCode" to authState.errorCode,
        "message" to authState.message,
    )).toString()

    @JvmStatic
    fun notifyInteraction(kind: String): Boolean = InteractionNotifier.notify(kind)

    @JvmStatic
    fun clearInteraction(): Boolean = InteractionNotifier.clear()

    @JvmStatic
    fun cancelAuthorizeJson(): String {
        cancelAuth()
        return JSONObject(mapOf("ok" to true, "message" to "已取消当前授权流程")).toString()
    }

    @JvmStatic
    fun authorize(onUrl: (String) -> Unit): AuthResult {
        val started = beginAuth(onUrl)
        if (!started.ok) return started
        val deadline = System.currentTimeMillis() + LOGIN_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            val state = authState
            if (state.finished) {
                return AuthResult(state.ok, state.url, state.errorCode, state.message)
            }
            Thread.sleep(200)
        }
        cancelAuth()
        return AuthResult(false, authState.url, "TIMEOUT", "授权等待超时")
    }

    private fun beginAuth(onUrl: ((String) -> Unit)?): AuthResult {
        val ctx = appContext ?: return AuthResult(
            false, errorCode = "RUNTIME_UNAVAILABLE", message = "WpsRuntime 未初始化")
        synchronized(lock) {
            cancelAuthLocked()
            val wpsDir = wpsDir(ctx)
            val urlFile = File(wpsDir, AUTH_URL_FILE).apply { delete() }
            authState = AuthState(running = true)
            val coordinator = AuthCoordinator(ctx, urlFile) { url ->
                authState = authState.copy(url = url)
                onUrl?.invoke(url)
            }
            authCoordinator = coordinator
            coordinator.start()

            val thread = Thread {
                try {
                    val result = runCommand(
                        listOf("auth", "login", "--oauth-timeout", LOGIN_TIMEOUT_MS.toString()),
                        null,
                        LOGIN_TIMEOUT_MS,
                        onProcess = { authProcess = it },
                    )
                    val status = authStatus()
                    val ok = status.authenticated
                    authState = authState.copy(
                        running = false,
                        finished = true,
                        ok = ok,
                        authenticated = status.authenticated,
                        errorCode = if (ok) null else (result.errorCode ?: status.errorCode),
                        message = if (ok) "授权成功" else (
                            status.message.ifBlank { result.stderr.take(300) }),
                    )
                } catch (exc: Throwable) {
                    authState = authState.copy(
                        running = false,
                        finished = true,
                        ok = false,
                        errorCode = "RUNTIME_CRASHED",
                        message = "${exc.javaClass.simpleName}: ${exc.message}",
                    )
                } finally {
                    authProcess = null
                    authCoordinator?.stop()
                    authCoordinator = null
                }
            }
            authThread = thread
            thread.start()
            return AuthResult(true, message = "已启动 WPS 授权流程")
        }
    }

    @JvmStatic
    fun logout(): ExecResult {
        val result = runCommand(listOf("auth", "logout"), null, 60_000, onProcess = null)
        authState = AuthState()
        return result
    }

    private fun cancelAuth() {
        synchronized(lock) {
            cancelAuthLocked()
        }
    }

    private fun cancelAuthLocked() {
        try {
            authProcess?.destroyForcibly()
        } catch (_: Throwable) {
            // ignore
        }
        authProcess = null
        authCoordinator?.stop()
        authCoordinator = null
        if (authState.running) {
            authState = AuthState(finished = true, errorCode = "CANCELLED", message = "授权已取消")
        }
    }

    // ------------------------------------------------------------------
    // 外链 / 诊断
    // ------------------------------------------------------------------
    @JvmStatic
    fun openExternal(url: String): Boolean {
        val ctx = appContext ?: return false
        if (!url.startsWith("https://") && !url.startsWith("http://")) return false
        val uri = Uri.parse(url)
        return try {
            val intent = CustomTabsIntent.Builder().build().intent
            intent.data = uri
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(intent)
            true
        } catch (_: Throwable) {
            try {
                ctx.startActivity(
                    Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                true
            } catch (_: Throwable) {
                false
            }
        }
    }

    @JvmStatic
    fun diagnosticsMap(): Map<String, Any?> {
        val ctx = appContext
        val result = linkedMapOf<String, Any?>(
            "ok" to (ctx != null),
            "sdk" to Build.VERSION.SDK_INT,
            "supportedAbis" to Build.SUPPORTED_ABIS.toList(),
            "nativeLibraryDir" to (ctx?.applicationInfo?.nativeLibraryDir ?: nativeLibraryDir),
            "executionDir" to (ctx?.let { effectiveBinaryDir(it).absolutePath }),
            "executionMode" to (ctx?.let {
                if (WpsRuntimeInternals.shouldUseFilesDirFallback(
                        it.applicationInfo.targetSdkVersion)) "filesDir" else "nativeLibraryDir"
            }),
            "filesDir" to ctx?.filesDir?.absolutePath,
            "wpsDir" to ctx?.let { wpsDir(it).absolutePath },
            "targetSdk" to (ctx?.applicationInfo?.targetSdkVersion ?: Build.VERSION.SDK_INT),
            "serverRunning" to (authThread?.isAlive == true),
        )
        if (ctx != null) {
            result["binaries"] = runtimeBinaryFiles(ctx).associate {
                it.name to mapOf("exists" to it.isFile, "size" to it.length(),
                    "sha256" to sha256Short(it))
            }
            val resolv = File(wpsDir(ctx), "resolv.conf")
            result["resolvConf"] = mapOf(
                "exists" to resolv.isFile,
                "content" to runCatching { resolv.readText().trim() }.getOrDefault(""),
                "dns" to readDnsServers(ctx),
            )
            val ca = File(wpsDir(ctx), "cacert.pem")
            result["caBundle"] = mapOf("exists" to ca.isFile, "size" to ca.length(),
                "sha256" to sha256Short(ca))
        } else {
            result["errorCode"] = "RUNTIME_UNAVAILABLE"
        }
        return result
    }

    private fun sha256Short(file: File): String {
        if (!file.isFile) return ""
        return try {
            val digest = MessageDigest.getInstance("SHA-256")
            val bytes = digest.digest(file.readBytes())
            Base64.encodeToString(bytes, Base64.NO_WRAP).take(16)
        } catch (_: Throwable) {
            ""
        }
    }

    private fun toJson(value: Any?): String = when (value) {
        null -> "null"
        is RuntimeStatus -> JSONObject(mapOf(
            "ok" to value.ok,
            "errorCode" to value.errorCode,
            "message" to value.message,
            "nativeLibraryDir" to value.nativeLibraryDir,
            "details" to value.details,
        )).toString()
        is ExecResult -> JSONObject(mapOf(
            "exitCode" to value.exitCode,
            "stdout" to value.stdout,
            "stderr" to value.stderr,
            "timedOut" to value.timedOut,
            "errorCode" to value.errorCode,
        )).toString()
        is AuthStatus -> JSONObject(mapOf(
            "authenticated" to value.authenticated,
            "errorCode" to value.errorCode,
            "message" to value.message,
        )).toString()
        is AuthResult -> JSONObject(mapOf(
            "ok" to value.ok,
            "url" to value.url,
            "errorCode" to value.errorCode,
            "message" to value.message,
        )).toString()
        else -> value.toString()
    }

}
