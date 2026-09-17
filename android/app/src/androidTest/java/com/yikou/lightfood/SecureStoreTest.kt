package com.yikou.lightfood

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Android Keystore 真机/模拟器测试；`./gradlew connectedDebugAndroidTest` 执行。 */
@RunWith(AndroidJUnit4::class)
class SecureStoreTest {
    @Test
    fun roundTripAndDelete() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        SecureStore.initialize(context)
        val service = "secure-store-test-service"
        val user = "secure-store-test-user"

        assertTrue(SecureStore.setSecret(service, user, "pa55w0rd"))
        assertEquals("pa55w0rd", SecureStore.getSecret(service, user))
        assertTrue(SecureStore.deleteSecret(service, user))
        assertNull(SecureStore.getSecret(service, user))
    }

    @Test
    fun emptyUsernameNeverWrites() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        SecureStore.initialize(context)
        assertFalse(SecureStore.setSecret("s", "", "x"))
        assertFalse(SecureStore.deleteSecret("s", ""))
        assertNull(SecureStore.getSecret("s", ""))
    }
}
