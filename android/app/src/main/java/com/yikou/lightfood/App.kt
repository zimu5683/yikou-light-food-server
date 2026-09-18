package com.yikou.lightfood

import android.app.Application

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        // 这些静态方法会被 Chaquopy/Python 调用，必须先拿到 application context。
        SecureStore.initialize(this)
        AdminWebLogin.initialize(this)
    }
}
