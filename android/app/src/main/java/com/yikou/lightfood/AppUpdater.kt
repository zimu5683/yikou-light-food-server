package com.yikou.lightfood

import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * APK 应用内更新原生桥。
 *
 * 设计约束：
 * * Python（Bridge）负责从 GitHub Releases 取版本、下载和 SHA-256 校验；
 * * Kotlin 只负责「最后 1 米」：校验安装包签名、检查未知来源安装权限、调用
 *   系统 PackageInstaller；
 * * 任何失败都返回 JSON 给 Python，由前端展示，不再打开浏览器兜底。
 *
 * 覆盖安装要求新旧 APK 使用同一把签名证书；CI 从 YIKOU_KEYSTORE_* secrets
 * 读取，本地缺失时会回退 debug 签名，此时正式包无法覆盖自更新。
 */
object AppUpdater {
    @Volatile
    private var appContext: Context? = null

    @JvmStatic
    fun initialize(context: Context) {
        appContext = context.applicationContext
    }

    /** 返回 Python 可解码的能力 JSON：版本号 + 是否已允许安装未知来源应用。 */
    @JvmStatic
    fun updateCapabilitiesJson(): String {
        val context = appContext
            ?: return errorJson("RUNTIME_UNAVAILABLE", "更新模块尚未初始化")
        return try {
            val info = context.packageManager.getPackageInfo(context.packageName, 0)
            JSONObject().apply {
                put("ok", true)
                put("canInstall", context.packageManager.canRequestPackageInstalls())
                put("versionName", info.versionName ?: "")
                put("versionCode", versionCodeOf(info))
            }.toString()
        } catch (exc: Throwable) {
            errorJson("VERSION_UNAVAILABLE", "读取当前版本失败：${exc.message}")
        }
    }

    /**
     * 校验待安装 APK：包名必须一致，签名必须与当前 App 一致。
     *
     * 注意：签名校验只能确认“能覆盖安装”，不能确认版本号一定更新；versionCode
     * 由 Python 侧结合 capabilities 再比较一次。
     */
    @JvmStatic
    fun verifyUpdateApkJson(path: String): String {
        val context = appContext
            ?: return errorJson("RUNTIME_UNAVAILABLE", "更新模块尚未初始化")
        return try {
            val apk = File(path)
            if (!apk.isFile) {
                return errorJson("APK_NOT_FOUND", "安装包不存在或已失效")
            }
            val archive = archivePackageInfo(context, apk.absolutePath)
                ?: return errorJson("APK_INVALID", "无法解析安装包，文件可能已损坏")
            if (archive.packageName != context.packageName) {
                return errorJson("PACKAGE_MISMATCH", "安装包包名与当前应用不一致")
            }
            val installed = context.packageManager.getPackageInfo(
                context.packageName, packageInfoFlags())
            val archiveDigests = signatureDigests(archive)
            val installedDigests = signatureDigests(installed)
            val sameSignature = archiveDigests.isNotEmpty() && archiveDigests == installedDigests
            JSONObject().apply {
                put("ok", true)
                put("packageName", archive.packageName)
                put("versionName", archive.versionName ?: "")
                put("versionCode", versionCodeOf(archive))
                put("sameSignature", sameSignature)
                if (!sameSignature) {
                    put("message", "安装包签名与当前应用不一致")
                }
            }.toString()
        } catch (exc: Throwable) {
            errorJson("APK_VERIFY_FAILED", "校验安装包失败：${exc.message}")
        }
    }

    /** 用 FileProvider 把安装包交给系统 PackageInstaller。 */
    @JvmStatic
    fun installApkJson(path: String): String {
        val context = appContext
            ?: return errorJson("RUNTIME_UNAVAILABLE", "更新模块尚未初始化")
        return try {
            val updatesDir = File(context.cacheDir, "updates").canonicalFile
            val apk = File(path).canonicalFile
            if (!apk.isFile) {
                return errorJson("APK_NOT_FOUND", "安装包不存在或已失效")
            }
            val allowed = apk.path.startsWith(updatesDir.path + File.separator)
            if (!allowed) {
                return errorJson("INVALID_PATH", "安装包路径不在允许范围内")
            }
            if (!context.packageManager.canRequestPackageInstalls()) {
                return JSONObject().apply {
                    put("ok", false)
                    put("code", "permission_required")
                    put("message", "请先允许本应用安装「未知来源应用」")
                }.toString()
            }
            val uri: Uri = FileProvider.getUriForFile(
                context, "${context.packageName}.fileprovider", apk)
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            JSONObject().apply {
                put("ok", true)
                put("message", "已打开系统安装器")
            }.toString()
        } catch (exc: Throwable) {
            errorJson("INSTALL_ACTIVITY_FAILED", "拉起系统安装器失败：${exc.message}")
        }
    }

    /** 打开「安装未知应用」授权页；失败时回退到设置首页。 */
    @JvmStatic
    fun openInstallPermissionSettingsJson(): String {
        val context = appContext
            ?: return errorJson("RUNTIME_UNAVAILABLE", "更新模块尚未初始化")
        return try {
            val packageIntent = Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${context.packageName}"),
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(packageIntent)
            JSONObject().apply { put("ok", true) }.toString()
        } catch (_: Throwable) {
            try {
                context.startActivity(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                JSONObject().apply { put("ok", true) }.toString()
            } catch (exc: Throwable) {
                errorJson("SETTINGS_UNAVAILABLE", "无法打开安装权限设置：${exc.message}")
            }
        }
    }

    // ------------------------------------------------------------------
    // 纯函数 / 兼容旧测试
    // ------------------------------------------------------------------

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

    @Suppress("DEPRECATION")
    private fun versionCodeOf(info: PackageInfo): Long =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            info.versionCode.toLong()
        }

    @Suppress("DEPRECATION")
    private fun packageInfoFlags(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            PackageManager.GET_SIGNING_CERTIFICATES
        } else {
            PackageManager.GET_SIGNATURES
        }

    @Suppress("DEPRECATION")
    private fun archivePackageInfo(context: Context, path: String): PackageInfo? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            context.packageManager.getPackageArchiveInfo(
                path, PackageManager.GET_SIGNING_CERTIFICATES)
        } else {
            context.packageManager.getPackageArchiveInfo(path, PackageManager.GET_SIGNATURES)
        }

    private fun signatureDigests(info: PackageInfo): Set<String> {
        val signatures: Array<android.content.pm.Signature> =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                val signingInfo = info.signingInfo ?: return emptySet()
                if (signingInfo.hasMultipleSigners()) {
                    signingInfo.apkContentsSigners
                } else {
                    val certificate = signingInfo.signingCertificate ?: return emptySet()
                    arrayOf(certificate)
                }
            } else {
                info.signatures ?: return emptySet()
            }
        return signatures.mapNotNull { signature ->
            MessageDigest.getInstance("SHA-256")
                .digest(signature.toByteArray())
                .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
        }.toSet()
    }

    private fun errorJson(code: String, message: String): String =
        JSONObject().apply {
            put("ok", false)
            put("code", code)
            put("message", message)
        }.toString()
}
