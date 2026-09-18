/**
 * 手机端日志「水波扩散」的开合状态机。
 *
 * 日志面板是**全屏覆盖层**：
 * - 点右上角日志按钮或任务启动时，从触发按钮圆心做圆形水波展开；
 * - 再次点日志按钮，从日志按钮圆心做反向水波收回，露出原来的界面；
 * - 没有底部抽屉、没有高度拖拽，也没有自下而上的翻滚。
 *
 * 圆心与开合状态由共同父级（App）持有：展开那一刻面板才能读到正确的圆心，
 * 按钮/操作栏也只需要调用这里的回调。
 */
import { useCallback, useRef, useState, type RefObject } from 'react'

import { FAB, centerOfRect, resolveOrigin, type Point } from '@/lib/reveal'

export interface LogReveal {
  /** 本次水波的圆心（视口坐标）。 */
  origin: Point
  /** 每展开一次自增；面板据此触发一次扩散动画（0 = 从未展开过）。 */
  nonce: number
  /** 日志是否处于展开状态。 */
  open: boolean
  /** 立刻展开：记录圆心 + 打开面板。已展开时只更新圆心，不重播水波。 */
  openFrom: (from: Point | null) => void
  /** 只记圆心、不展开（等任务真的起来再 `openRemembered`）。 */
  rememberFrom: (from: Point | null) => void
  /** 用记住的圆心展开（没记住就用日志按钮中心）。 */
  openRemembered: () => void
  /** 日志按钮：开→关，关→开。 */
  toggleFrom: (from: Point | null) => void
}

/** 日志按钮量不到时的兜底圆心：固定在右上角。 */
function fallbackFabCenter(): Point {
  if (typeof window === 'undefined') return { x: 0, y: 0 }
  return {
    x: window.innerWidth - FAB.rightPx - FAB.sizePx / 2,
    y: FAB.fallbackTopPx + FAB.sizePx / 2,
  }
}

export function useLogReveal(fabRef: RefObject<HTMLElement | null>): LogReveal {
  const [origin, setOrigin] = useState<Point>(fallbackFabCenter)
  const [nonce, setNonce] = useState(0)
  const [open, setOpen] = useState(false)
  /** 「开始处理」按下时记下的圆心，等任务起来再用。 */
  const remembered = useRef<Point | null>(null)

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
      if (open) return
      setNonce((current) => current + 1)
      setOpen(true)
    },
    [fabCenter, open],
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
      if (open) {
        // 收起必须从右上角日志按钮圆心出发，和展开方向相反。
        setOrigin(fabCenter())
        setOpen(false)
        return
      }
      openFrom(from)
    },
    [fabCenter, open, openFrom],
  )

  return { origin, nonce, open, openFrom, rememberFrom, openRemembered, toggleFrom }
}
