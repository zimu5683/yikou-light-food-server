/**
 * 手机端日志「水波扩散」的开合状态机。
 *
 * 日志面板是全屏覆盖层：
 * - 圆心不在这里计算，也不来自任何点击坐标；
 * - 面板的 clip-path keyframes 使用 `LOG_REVEAL_ORIGIN`，由 CSS 直接定位
 *   右上角日志按钮中心；
 * - 展开/收回都只是给面板一个 nonce/开关信号。
 *
 * 这里仍然负责开合状态，让悬浮按钮、「开始处理/开始下单」「确认上传」等
 * 多个入口共用同一条开合动作。
 */
import { useCallback, useState } from 'react'

import type { Point } from '@/lib/reveal'

export interface LogReveal {
  /** 每展开一次自增；面板据此触发一次扩散动画（0 = 从未展开过）。 */
  nonce: number
  /** 日志是否处于展开状态。 */
  open: boolean
  /** 立刻展开。参数保留兼容旧调用方；圆心由 CSS 决定，不使用该坐标。 */
  openFrom: (from: Point | null) => void
  /** 旧接口保留：不再记录圆心。 */
  rememberFrom: (from: Point | null) => void
  /** 任务起来后自动展开。 */
  openRemembered: () => void
  /** 日志按钮：开→关，关→开。 */
  toggleFrom: (from: Point | null) => void
}

export function useLogReveal(): LogReveal {
  const [nonce, setNonce] = useState(0)
  const [open, setOpen] = useState(false)

  const openFrom = useCallback((_from: Point | null) => {
    if (open) return
    setNonce((current) => current + 1)
    setOpen(true)
  }, [open])

  const rememberFrom = useCallback((_from: Point | null) => {
    // 旧接口保留：圆心完全由 CSS 决定。
  }, [])

  const openRemembered = useCallback(() => {
    openFrom(null)
  }, [openFrom])

  const toggleFrom = useCallback((from: Point | null) => {
    if (open) {
      setOpen(false)
      return
    }
    openFrom(from)
  }, [open, openFrom])

  return { nonce, open, openFrom, rememberFrom, openRemembered, toggleFrom }
}
