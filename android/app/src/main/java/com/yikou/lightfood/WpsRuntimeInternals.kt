package com.yikou.lightfood

/**
 * WpsRuntime 的纯函数部分，刻意不依赖 Android Context/Process，便于 JVM 单测。
 * 真机执行路径在 WpsRuntime.runCommand() 中，只负责把这里的字符串/命令接过去。
 */
object WpsRuntimeInternals {
    /** DNS 完全读不到时的公共解析器兜底（VPN/fake-ip 太复杂时人工可改 resolv 文件）。 */
    val DEFAULT_DNS: List<String> = listOf("223.5.5.5", "119.29.29.29")

    /** APK 内所有运行时组件文件名。 */
    val RUNTIME_BINARY_NAMES: List<String> = listOf(
        "libproot.so",
        "libproot_loader.so",
        "libtalloc.so",
        "libandroid-shmem.so",
        "libkdocs_cli.so",
        "libxdgopen_shim.so",
    )

    /**
     * M0 停机点：targetSdk >= 29 时 Android 的 W^X 会阻止从应用数据目录执行，
     * 因此只有当 APK 显式降级到 targetSdk <= 28 时才复制到 filesDir 执行。
     */
    fun shouldUseFilesDirFallback(targetSdk: Int): Boolean = targetSdk in 1..28

    /**
     * proot 内部 PATH。
     *
     * ``/usr/bin`` 必须排第一：kdocs-cli 用 ``exec.LookPath("xdg-open")`` 找浏览器，
     * 而我们的 shim 正好 bind 到 guest 的 ``/usr/bin/xdg-open``。实测若 PATH 没有
     * 这个目录，CLI 会去 PATH 其它目录找，导致授权 URL 写不进 auth_url.txt。
     */
    const val RUNTIME_PATH: String = "/usr/bin:/system/bin:/system/xbin:/product/bin"

    /** 拼出 proot 启动参数；所有路径都必须是绝对路径。 */
    fun buildCommand(
        prootPath: String,
        kdocsPath: String,
        shimPath: String,
        resolvConfPath: String,
        args: List<String>,
        paramsFile: String? = null,
    ): List<String> {
        val command = mutableListOf<String>()
        command.add(prootPath)
        command.addAll(listOf("-b", "$resolvConfPath:/etc/resolv.conf"))
        command.addAll(listOf("-b", "$shimPath:/usr/bin/xdg-open"))
        command.add(kdocsPath)
        command.addAll(args)
        if (paramsFile != null) {
            command.addAll(listOf("--file", paramsFile))
        }
        return command
    }

    /** 授权 URL 只允许 http/https，防止 xdg shim 写入 file:// 等被拉起。 */
    fun isSupportedAuthUrl(url: String): Boolean {
        return url.startsWith("https://", ignoreCase = true) ||
            url.startsWith("http://", ignoreCase = true)
    }

    /**
     * 清洗 ConnectivityManager 读到的 DNS：去重、过滤 link-local / 带 zone 的地址；
     * 一个都没有时返回公共 fallback，保证 resolv.conf 不为空。
     */
    fun normalizeDnsServers(candidates: List<String>): List<String> {
        val result = LinkedHashSet<String>()
        for (candidate in candidates) {
            val host = candidate.trim()
            if (host.isEmpty()) continue
            if (host.contains('%')) continue
            if (host.startsWith("fe80:", ignoreCase = true)) continue
            result.add(host)
        }
        return if (result.isEmpty()) DEFAULT_DNS else result.toList()
    }

    /** 写 resolv.conf 的格式；注意写入前必须过滤掉带 zone 的 IPv6 link-local。 */
    fun composeResolvConf(dnsServers: List<String>): String {
        val lines = dnsServers
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .distinct()
            .map { "nameserver $it" }
            .toMutableList()
        lines.add("options timeout:2 attempts:2")
        return lines.joinToString("\n", postfix = "\n")
    }

    /**
     * 把 proot / CLI 的非零退出与 stderr 映射为设计文档 §3.7 的 errorCode。
     * 只做保守判断；拿不准时返回 RUNTIME_CRASHED，调用方仍会展示原始 stderr。
     */
    fun classifyCliFailure(
        exitCode: Int,
        stdout: String,
        stderr: String,
        timedOut: Boolean,
    ): String? {
        if (timedOut) return "TIMEOUT"
        val combined = (stderr + "\n" + stdout).lowercase()
        if (combined.contains("bad system call") || combined.contains("sig sys")) {
            return "SECCOMP_BLOCKED"
        }
        if (combined.contains("x509") ||
            combined.contains("certificate signed by unknown") ||
            combined.contains("tls: failed to verify certificate")
        ) {
            return "TLS_CA_FAILED"
        }
        if (combined.contains("lookup ") &&
            (combined.contains("refused") || combined.contains("no such host") ||
                combined.contains("i/o timeout"))
        ) {
            return "DNS_FAILED"
        }
        if (exitCode != 0) return "RUNTIME_CRASHED"
        return null
    }
}
