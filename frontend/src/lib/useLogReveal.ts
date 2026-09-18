/**
 * 手机端日志「水波扩散」的开合状态机。
 *
 * 为什么放在 hook 而不是面板组件内部：**扩散的圆心来自面板之外** ——
 * 悬浮按钮、操作栏的「开始处理/开始下单」、云文档的「确认上传」都要把
 * 自己的位置交给面板。圆心与「第几次展开」必须由共同父级（App）持有，
 * 面板才可能在展开那一刻读到正确的那一份（放在面板里就得把 ref 反向传出去，
 * 按钮拿到的会是上一次渲染的闭包）。
 *
 * ## 三种触发路径，一条展开动作
 *
 * 1. `toggleFrom` —— 悬浮按钮：关着就展开，开着就收起；
 * 2. `rememberFrom` + `openRemembered` —— 「开始处理/开始下单」：**点击时只记
 *    圆心**，等任务真的跑起来（`workerAlive`）再展开，表单校验失败就不展开；
 * 3. `openFrom` —— 「确认上传」：它是同步接口，点下去就该看到日志。
 *
 * 圆心优先用点击点，取不到（任务由另一台设备启动、坐标非法）时退回悬浮按钮
 * 中心 —— 见 `lib/reveal.ts` 的 `resolveOrigin`。
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

import { FAB, centerOfRect, resolveOrigin, type Point } from '@/lib/reveal'
import { LOG_SHEET } from '@/lib/logSheet'
import type { LogSheetDrag } from '@/lib/useLogSheetDrag'

export interface LogReveal {
  /** 本次扩散的圆心（视口坐标）。 */
  origin: Point
  /** 每展开一次自增；面板据此触发一次扩散动画（0 = 从未展开过）。 */
  nonce: number
  /** 立刻展开：记录圆心 + 打开面板。已展开时只更新圆心，不重播水波。 */
  openFrom: (from: Point | null) => void
  /** 只记圆心、不展开（等任务真的起来再 `openRemembered`）。 */
  rememberFrom: (from: Point | null) => void
  /** 用记住的圆心展开（没记住就用悬浮按钮中心）。 */
  openRemembered: () => void
  /** 悬浮按钮：开→关，关→开。 */
  toggleFrom: (from: Point | null) => void
}

/** 悬浮按钮量不到时的兜底圆心：按「收起状态下的落点」估算。 */
function fallbackFabCenter(): Point {
  if (typeof window === 'undefined') return { x: 0, y: 0 }
  const bottom = LOG_SHEET.peekPx + FAB.fallbackDockPx + FAB.gapPx
  return {
    x: window.innerWidth - FAB.rightPx - FAB.sizePx / 2,
    y: window.innerHeight - bottom - FAB.sizePx / 2,
  }
}

export function useLogReveal(
  drag: LogSheetDrag,
  fabRef: RefObject<HTMLElement | null>,
): LogReveal {
  const [origin, setOrigin] = useState<Point>(fallbackFabCenter)
  const [nonce, setNonce] = useState(0)
  /** 「开始处理」按下时记下的圆心，等任务起来再用。 */
  const remembered = useRef<Point | null>(null)
  /**
   * 镜像最新的开合状态。
   *
   * 不直接用 `drag.open` 进依赖数组：那会让下面每个 callback 在开合时换一次
   * 引用，App 里「任务起来就展开」的 effect 会跟着重跑（虽然被 `wasRunning`
   * 挡住不会出错，但没必要）。
   */
  const openRef = useRef(drag.open)
  const setOpen = drag.setOpen

  useEffect(() => {
    openRef.current = drag.open
  }, [drag.open])

  const fabCenter = useCallback((): Point => {
    const element = fabRef.current
    if (element) {
      const rect = element.getBoundingClientRect()
      if (rect.width > 0 || rect.height > 0) return centerOfRect(rect)
    }
    return fallbackFabCenter()
  }, [fabRef])

  const openFrom = useCallback(
    (from: Point | null) => {
      setOrigin(resolveOrigin(from, fabCenter()))
      if (openRef.current) return
      setNonce((current) => current + 1)
      setOpen(true)
    },
    [fabCenter, setOpen],
  )

  const rememberFrom = useCallback((from: Point | null) => {
    remembered.current = from
  }, [])

  const openRemembered = useCallback(() => {
    const from = remembered.current
    remembered.current = null
    openFrom(from)
  }, [openFrom])

  const toggleFrom = useCallback(
    (from: Point | null) => {
      if (openRef.current) {
        setOpen(false)
        return
      }
      openFrom(from)
    },
    [openFrom, setOpen],
  )

  return { origin, nonce, openFrom, rememberFrom, openRemembered, toggleFrom }
}
