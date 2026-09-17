package com.yikou.lightfood

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WpsRuntimeInternalsTest {

    @Test
    fun buildCommand_containsProotBindsAndParamsFile() {
        val command = WpsRuntimeInternals.buildCommand(
            prootPath = "/data/app/lib/arm64/libproot.so",
            kdocsPath = "/data/app/lib/arm64/libkdocs_cli.so",
            shimPath = "/data/app/lib/arm64/libxdgopen_shim.so",
            resolvConfPath = "/data/user/0/app/files/wps/resolv.conf",
            args = listOf("sheet", "get-range-data"),
            paramsFile = "/data/user/0/app/files/wps/tmp/x.json",
        )

        assertEquals("/data/app/lib/arm64/libproot.so", command[0])
        assertEquals("-b", command[1])
        assertTrue(command[2].endsWith("resolv.conf:/etc/resolv.conf"))
        assertEquals("-b", command[3])
        assertTrue(command[4].endsWith("libxdgopen_shim.so:/usr/bin/xdg-open"))
        assertEquals("/data/app/lib/arm64/libkdocs_cli.so", command[5])
        assertEquals(listOf("sheet", "get-range-data"), command.subList(6, 8))
        assertEquals(listOf("--file", "/data/user/0/app/files/wps/tmp/x.json"),
            command.subList(8, 10))
    }

    @Test
    fun buildCommand_omitsFileWhenNoParams() {
        val command = WpsRuntimeInternals.buildCommand(
            prootPath = "proot",
            kdocsPath = "kdocs",
            shimPath = "shim",
            resolvConfPath = "resolv",
            args = listOf("auth", "status"),
        )
        assertEquals(listOf("auth", "status"), command.takeLast(2))
        assertFalse(command.contains("--file"))
    }

    @Test
    fun composeResolvConf_writesNameserversAndOptions() {
        val text = WpsRuntimeInternals.composeResolvConf(
            listOf("8.8.8.8", "8.8.8.8", "", "2001:4860:4860::8888")
        )
        assertEquals(
            "nameserver 8.8.8.8\nnameserver 2001:4860:4860::8888\n" +
                "options timeout:2 attempts:2\n",
            text,
        )
    }

    @Test
    fun normalizeDnsServers_filtersScopedAndLinkLocalAndFallsBack() {
        val normalized = WpsRuntimeInternals.normalizeDnsServers(
            listOf("8.8.8.8", "2001:db8::1", "fe80::1%wlan0", "fe80::2", "", "1.1.1.1")
        )
        assertEquals(listOf("8.8.8.8", "2001:db8::1", "1.1.1.1"), normalized)

        assertEquals(WpsRuntimeInternals.DEFAULT_DNS,
            WpsRuntimeInternals.normalizeDnsServers(listOf("fe80::1")))
    }

    @Test
    fun classifyCliFailure_mapsKnownCodes() {
        assertEquals("TIMEOUT",
            WpsRuntimeInternals.classifyCliFailure(-1, "", "", timedOut = true))
        assertEquals("SECCOMP_BLOCKED",
            WpsRuntimeInternals.classifyCliFailure(159, "", "SIGSYS: bad system call", false))
        assertEquals("TLS_CA_FAILED",
            WpsRuntimeInternals.classifyCliFailure(1, "",
                "x509: certificate signed by unknown authority", false))
        assertEquals("DNS_FAILED",
            WpsRuntimeInternals.classifyCliFailure(1, "",
                "lookup mcp-center.wps.cn: connection refused", false))
        assertEquals("RUNTIME_CRASHED",
            WpsRuntimeInternals.classifyCliFailure(2, "", "boom", false))
        assertNull(WpsRuntimeInternals.classifyCliFailure(0, "{}", "", false))
    }

    @Test
    fun filesDirFallback_onlyEnabledForOldTargetSdk() {
        assertTrue(WpsRuntimeInternals.shouldUseFilesDirFallback(28))
        assertFalse(WpsRuntimeInternals.shouldUseFilesDirFallback(29))
        assertFalse(WpsRuntimeInternals.shouldUseFilesDirFallback(35))
    }

    @Test
    fun runtimeBinaryNames_matchFetchScriptOutput() {
        assertEquals(
            listOf("libproot.so", "libproot_loader.so", "libtalloc.so",
                "libandroid-shmem.so", "libkdocs_cli.so", "libxdgopen_shim.so"),
            WpsRuntimeInternals.RUNTIME_BINARY_NAMES,
        )
    }

    @Test
    fun runtimePath_putsBindShimDirectoryFirst() {
        val entries = WpsRuntimeInternals.RUNTIME_PATH.split(":")
        assertEquals("/usr/bin", entries.first())
        assertTrue(entries.contains("/system/bin"))
    }

    @Test
    fun isSupportedAuthUrl_rejectsNonHttpSchemes() {
        assertTrue(WpsRuntimeInternals.isSupportedAuthUrl("https://example.invalid/auth"))
        assertTrue(WpsRuntimeInternals.isSupportedAuthUrl("http://127.0.0.1/cb"))
        assertFalse(WpsRuntimeInternals.isSupportedAuthUrl("file:///etc/passwd"))
        assertFalse(WpsRuntimeInternals.isSupportedAuthUrl("javascript:alert(1)"))
        assertFalse(WpsRuntimeInternals.isSupportedAuthUrl(""))
    }
}
