package com.yikou.lightfood

import android.app.Application

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        // 这些静态方法会被 Chaquopy/Python 调用，必须先拿到 application context。
        SecureStore.initialize(this)
        AdminWebLogin.initialize(this)
        // APK 自更新原生桥：Python 下载后用这里的方法校验并拉起系统安装器。
        AppUpdater.initialize(this)
    }
}
