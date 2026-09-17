package com.yikou.lightfood

import java.util.concurrent.CopyOnWriteArrayList

/** Activity / Service 之间共享的运行时快照。 */
data class RuntimeSnapshot(
    val state: String = "starting",
    val serverUrl: String = "",
    val port: Int = 0,
    val message: String = "",
    val diagnostics: String = "",
    val warning: String = "",
)

/** 进程内状态总线；不依赖 AndroidX Lifecycle，避免给 M0 增加耦合。 */
object RuntimeState {
    @Volatile
    private var snapshot = RuntimeSnapshot()

    private val listeners = CopyOnWriteArrayList<(RuntimeSnapshot) -> Unit>()

    fun addListener(listener: (RuntimeSnapshot) -> Unit) {
        listeners.add(listener)
        listener(snapshot)
    }

    fun removeListener(listener: (RuntimeSnapshot) -> Unit) {
        listeners.remove(listener)
    }

    val current: RuntimeSnapshot
        get() = snapshot

    fun update(snapshot: RuntimeSnapshot) {
        this.snapshot = snapshot
        for (listener in listeners) {
            try {
                listener(snapshot)
            } catch (_: Throwable) {
                // 单个界面回调失败不能影响 Service 启动流程。
            }
        }
    }

    fun serverReady(url: String, port: Int, warning: String = "") {
        update(RuntimeSnapshot(
            state = "server_ready", serverUrl = url, port = port, warning = warning))
    }

    fun error(message: String, diagnostics: String = "") {
        update(RuntimeSnapshot(state = "error", message = message, diagnostics = diagnostics))
    }
}
