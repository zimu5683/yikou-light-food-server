/**
 * 手机端日志「水波扩散」的开合状态机。
 *
 * 日志面板是**全屏覆盖层**：
 * - 点右上角日志按钮或任务启动时，都从右上角日志按钮圆心做圆形水波展开；
 * - 再次点日志按钮，从同一圆心做反向水波收回，露出原来的界面；
 * - 没有底部抽屉、没有高度拖拽，也没有自下而上的翻滚。
 *
 * 圆心与开合状态由共同父级（App）持有：展开那一刻面板才能读到正确的圆心，
 * 按钮/操作栏也只需要调用这里的回调。
 */
import { useCallback, useState, type RefObject } from 'react'

import { FAB, centerOfRect, type Point } from '@/lib/reveal'

export interface LogReveal {
  /** 本次水波的圆心（视口坐标）。 */
  origin: Point
  /** 每展开一次自增；面板据此触发一次扩散动画（0 = 从未展开过）。 */
  nonce: number
  /** 日志是否处于展开状态。 */
  open: boolean
  /** 立刻展开：圆心固定为右上角日志按钮。已展开时不重播水波。 */
  openFrom: (from: Point | null) => void
  /** 保留旧接口：现在不再使用点击位置作为圆心。 */
  rememberFrom: (from: Point | null) => void
  /** 任务起来后自动展开（同样从右上角日志按钮扩散）。 */
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
  const fabCenter = useCallback((): Point => {
    const element = fabRef.current
    if (element) {
      const rect = element.getBoundingClientRect()
      if (rect.width > 0 || rect.height > 0) return centerOfRect(rect)
    }
    return fallbackFabCenter()
  }, [fabRef])

  const openFrom = useCallback(
    (_from: Point | null) => {
      // 所有展开都从右上角日志按钮出发，不再使用「开始处理/确认上传」的点击点。
      // 保留参数仅为兼容既有调用方和「任务起来后自动展开」的 API。
      setOrigin(fabCenter())
      if (open) return
      setNonce((current) => current + 1)
      setOpen(true)
    },
    [fabCenter, open],
  )

  const rememberFrom = useCallback((_from: Point | null) => {
    // 旧接口保留：现在所有水波圆心都固定在右上角日志按钮。
  }, [])

  const openRemembered = useCallback(() => {
    // 「任务起来后自动展开」也统一从右上角日志按钮扩散。
    openFrom(null)
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
