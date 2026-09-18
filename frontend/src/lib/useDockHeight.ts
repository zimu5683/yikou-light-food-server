/**
 * 量出底部操作栏（`BottomDock`）的高度，供悬浮按钮定位。
 *
 * 悬浮按钮要**浮在操作栏上方**（压住「停止/更多」就点不到了），而操作栏高度
 * 不是常数：管理员多一行「更多」菜单（90px），普通用户没有（60px）；云文档
 * 页签压根没有操作栏。所以这里实测而不是写死。
 *
 * 实测的两个坑：
 * 1. 三个页签**常驻挂载**，非当前页签是 `display:none` —— 量到的是 0。
 *    因此取所有 dock 里的**最大可见高度**（`pickDockHeight`），而不是取第一个。
 * 2. 页签切换时元素在 `none`/`flex` 之间来回，尺寸会变 —— 用 `ResizeObserver`
 *    监听即可自动重算，不必把 `mode` 也塞进依赖。
 */
import { useEffect, useState } from 'react'

import { FAB, pickDockHeight } from '@/lib/reveal'

/** 操作栏容器上的标记属性；`TaskPanel` 的 BottomDock 负责带上它。 */
export const DOCK_ATTR = 'data-task-dock'

export function useDockHeight(active: boolean): number {
  // 显式写成 number：FAB 是 as const，直接推断会把状态锁死成字面量 90
  const [height, setHeight] = useState<number>(FAB.fallbackDockPx)

  useEffect(() => {
    if (!active || typeof document === 'undefined') return

    const docks = () => Array.from(document.querySelectorAll<HTMLElement>(`[${DOCK_ATTR}]`))
    const read = () => {
      setHeight(pickDockHeight(docks().map((dock) => dock.getBoundingClientRect().height)))
    }

    read()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(read)
    for (const dock of docks()) observer?.observe(dock)
    // 旋屏/地址栏伸缩也会改变可用高度，补一次（ResizeObserver 只盯元素自身）
    window.addEventListener('resize', read)
    window.addEventListener('orientationchange', read)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', read)
      window.removeEventListener('orientationchange', read)
    }
  }, [active])

  // 非手机布局不渲染悬浮按钮，值没有意义，给 0 免得误导调用方
  return active ? height : 0
}
