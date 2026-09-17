package com.yikou.lightfood

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Android Keystore + AES-GCM 密码存储。
 *
 * 数据放在应用私有 SharedPreferences 中，密文与随机 IV 以 base64 保存；密钥永不
 * 离开 Android Keystore。Python 侧只调用三个 @JvmStatic 方法，UI / 日志拿不到明文。
 */
object SecureStore {
    private const val PREFS_NAME = "yikou_secure_store"
    private const val KEY_ALIAS = "yikou_secure_store_v1"
    private const val ANDROID_KEYSTORE = "AndroidKeyStore"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val GCM_TAG_BITS = 128
    private const val IV_BYTES = 12

    @Volatile
    private var prefs: SharedPreferences? = null

    @JvmStatic
    fun initialize(context: Context) {
        if (prefs == null) {
            prefs = context.applicationContext
                .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        }
    }

    private fun store(): SharedPreferences? = prefs

    private fun entryKey(service: String, username: String): String {
        // XML SharedPreferences 不允许 NUL/控制字符做 key，base64(url-safe) 编码。
        val raw = "$service\n$username".toByteArray(StandardCharsets.UTF_8)
        return Base64.encodeToString(raw, Base64.NO_WRAP or Base64.URL_SAFE)
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let {
            return it.secretKey
        }
        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String? {
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            val iv = cipher.iv
            val encrypted = cipher.doFinal(plain.toByteArray(StandardCharsets.UTF_8))
            Base64.encodeToString(iv, Base64.NO_WRAP) + ":" +
                Base64.encodeToString(encrypted, Base64.NO_WRAP)
        } catch (_: Throwable) {
            null
        }
    }

    private fun decrypt(encoded: String): String? {
        return try {
            val parts = encoded.split(":", limit = 2)
            if (parts.size != 2) return null
            val iv = Base64.decode(parts[0], Base64.NO_WRAP)
            val data = Base64.decode(parts[1], Base64.NO_WRAP)
            if (iv.size != IV_BYTES) return null
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
            String(cipher.doFinal(data), StandardCharsets.UTF_8)
        } catch (_: Throwable) {
            null
        }
    }

    @JvmStatic
    fun getSecret(service: String, username: String): String? {
        if (username.isEmpty()) return null
        val encoded = store()?.getString(entryKey(service, username), null) ?: return null
        return decrypt(encoded)
    }

    @JvmStatic
    fun setSecret(service: String, username: String, password: String): Boolean {
        if (username.isEmpty()) return false
        val encoded = encrypt(password) ?: return false
        return store()?.edit()
            ?.putString(entryKey(service, username), encoded)
            ?.commit() == true
    }

    @JvmStatic
    fun deleteSecret(service: String, username: String): Boolean {
        if (username.isEmpty()) return false
        return store()?.edit()?.remove(entryKey(service, username))?.commit() == true
    }
}
