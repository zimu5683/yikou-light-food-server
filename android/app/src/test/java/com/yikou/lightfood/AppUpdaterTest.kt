package com.yikou.lightfood

import org.junit.Assert.assertEquals
import org.junit.Test

class AppUpdaterTest {
    @Test
    fun compareVersions_handlesVPrefixAndSegments() {
        assertEquals(0, AppUpdater.compareVersions("3.5.0", "v3.5.0"))
        assertEquals(1, AppUpdater.compareVersions("v3.6.0", "3.5.0"))
        assertEquals(-1, AppUpdater.compareVersions("v3.5.0", "3.5.1"))
        assertEquals(1, AppUpdater.compareVersions("4.0.0", "3.99.99"))
        assertEquals(-1, AppUpdater.compareVersions("not-version", "3.5.0"))
    }
}
