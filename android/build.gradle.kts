plugins {
    id("com.android.application") version "8.9.1" apply false
    id("org.jetbrains.kotlin.android") version "2.1.20" apply false
    // Chaquopy 17.0.0 支持 Python 3.10 - 3.14，与 AGP 8.9 兼容。
    id("com.chaquo.python") version "17.0.0" apply false
}
