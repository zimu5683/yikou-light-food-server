/**
 * 日志抽屉的拖拽状态机（手机端）。
 *
 * 为什么放在 hook 而不是抽屉组件内部：抽屉的把手**和**操作栏里的「日志」按钮
 * 都要能拖动它。若状态放在抽屉里、再把 API 通过 ref 交给按钮，按钮拿到的可能是
 * 上一次渲染的闭包（陈旧的起始高度/开合状态），拖动就会跳。把状态提到共同父级
 * （App）后，两边读到的永远是同一个当前值。
 *
 * 只依赖最基础的指针事件字段，便于测试与复用。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  LOG_SHEET,
  SHEET_STORAGE_KEY,
  clampHeight,
  dragToHeight,
  isClick,
  readStoredHeight,
  shouldCollapse,
} from '@/lib/logSheet'

/** 只要 clientY 即可，避免把 React 的事件类型带进来。 */
interface PointerLike {
  clientY: number
  currentTarget?: EventTarget | null
  pointerId?: number
}

export interface LogSheetDrag {
  open: boolean
  /** 抽屉当前高度（px）。收起时为 peek。 */
  height: number
  dragging: boolean
  setOpen: (next: boolean) => void
  toggle: () => void
  /** 拖拽手柄/按钮共用的事件处理器。 */
  onPointerDown: (event: PointerLike) => void
  onPointerMove: (event: PointerLike) => void
  onPointerUp: () => void
}

function readViewport(): number {
  return typeof window === 'undefined' ? 0 : window.innerHeight
}

function readStored(): string | null {
  if (typeof localStorage === 'undefined') return null
  try {
    return localStorage.getItem(SHEET_STORAGE_KEY)
  } catch {
    return null
  }
}

export function useLogSheetDrag(): LogSheetDrag {
  const [open, setOpen] = useState(false)
  const [viewportPx, setViewportPx] = useState(readViewport)
  /** 用户拖到的/记住的高度。不做吸附，拖到哪就是哪。 */
  const [height, setHeight] = useState(() => readStoredHeight(readStored(), readViewport()))
  const [dragging, setDragging] = useState(false)
  const start = useRef<{ y: number; height: number; moved: number } | null>(null)

  // 地址栏伸缩 / 旋屏改变视口高度：不重算把手可能被挤出屏幕。
  useEffect(() => {
    const sync = () => setViewportPx(readViewport())
    window.addEventListener('resize', sync)
    window.addEventListener('orientationchange', sync)
    return () => {
      window.removeEventListener('resize', sync)
      window.removeEventListener('orientationchange', sync)
    }
  }, [])

  const shownHeight = open ? clampHeight(height, viewportPx) : LOG_SHEET.peekPx

  const onPointerDown = useCallback(
    (event: PointerLike) => {
      start.current = { y: event.clientY, height: shownHeight, moved: 0 }
      setDragging(true)
      // 抓住指针：手指移出元素范围也继续收到 move/up，拖动才不会中断
      const target = event.currentTarget as HTMLElement | null
      if (target && event.pointerId !== undefined) {
        try {
          target.setPointerCapture(event.pointerId)
        } catch {
          /* 某些环境下不支持，忽略即可 */
        }
      }
    },
    [shownHeight],
  )

  const onPointerMove = useCallback((event: PointerLike) => {
    const state = start.current
    if (!state) return
    state.moved = Math.max(state.moved, Math.abs(event.clientY - state.y))
    setHeight(dragToHeight(state.height, state.y, event.clientY, readViewport()))
  }, [])

  const onPointerUp = useCallback(() => {
    const state = start.current
    start.current = null
    setDragging(false)
    if (!state) return

    // 位移很小 = 点击：切换开合
    if (isClick(state.moved)) {
      setOpen((current) => !current)
      return
    }
    // 拖到接近底部 = 收起；否则**原样保留**这个高度（用户要的是随意停）
    setHeight((current) => {
      if (shouldCollapse(current)) {
        setOpen(false)
        return current
      }
      setOpen(true)
      try {
        localStorage.setItem(SHEET_STORAGE_KEY, String(current))
      } catch {
        /* 隐私模式可能抛错，静默 */
      }
      return current
    })
  }, [])

  const toggle = useCallback(() => setOpen((current) => !current), [])

  return {
    open,
    height: shownHeight,
    dragging,
    setOpen,
    toggle,
    onPointerDown,
    onPointerMove,
    onPointerUp,
  }
}
