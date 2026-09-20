/**
 * 配置自动保存 hook。
 *
 * 通过 SaveCoordinator 实现：
 * - 每次编辑递增草稿版本；
 * - 同一时刻最多一个保存在途，连续点击/自动保存不会并发重复提交；
 * - 旧请求成功不会把更新的草稿标成已保存，而是合并后继续串行保存最新值；
 * - 失败保留最新草稿，重试总是保存最新版本；
 * - API 未就绪/网络错误不会显示“已保存”；
 * - 页面隐藏时触发立即冲刷；离开/卸载前有未保存内容时触发浏览器离开确认。
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { classifyRequestError } from '@/lib/requestError'
import {
  SaveCoordinator,
  type ConfigSaveResult,
  type SaveCoordinatorState,
  type SaveRunner,
} from '@/lib/saveState'

export function useConfigSave(
  save: () => Promise<ConfigSaveResult>,
  options: { delay?: number } = {},
) {
  const delay = options.delay ?? 500
  const saveRef = useRef(save)
  const [coordinator] = useState(() => new SaveCoordinator())
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    saveRef.current = save
  }, [save])

  const state: SaveCoordinatorState = useSyncExternalStore(
    coordinator.subscribe,
    coordinator.getState,
    coordinator.getState,
  )

  const clearTimer = useCallback(() => {
    if (timer.current !== undefined) {
      window.clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])

  const runner = useCallback<SaveRunner>(() => {
    return saveRef.current()
      .then((result) => ({
        ok: result?.ok !== false,
        reason: result?.reason,
        message: result?.message,
      }))
      .catch((error) => ({
        ok: false,
        message: classifyRequestError(error).detail || '保存失败',
      }))
  }, [])

  const submit = useCallback(async (): Promise<boolean> => {
    clearTimer()
    return coordinator.run(runner)
  }, [clearTimer, coordinator, runner])

  const schedule = useCallback(() => {
    coordinator.markEdited()
    clearTimer()
    timer.current = window.setTimeout(() => {
      timer.current = undefined
      void coordinator.run(runner)
    }, delay)
  }, [clearTimer, coordinator, delay, runner])

  const retry = useCallback((): Promise<boolean> => {
    clearTimer()
    return coordinator.run(runner)
  }, [clearTimer, coordinator, runner])

  useEffect(() => {
    const flushOnHidden = () => {
      if (document.visibilityState === 'hidden' && coordinator.hasUnsavedChanges()) {
        void coordinator.run(runner)
      }
    }
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (coordinator.hasUnsavedChanges()) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    document.addEventListener('visibilitychange', flushOnHidden)
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => {
      document.removeEventListener('visibilitychange', flushOnHidden)
      window.removeEventListener('beforeunload', warnBeforeUnload)
      clearTimer()
    }
  }, [clearTimer, coordinator, runner])

  return {
    state,
    revision: state.draftVersion,
    savedRevision: state.savedVersion,
    pending: state.pending,
    schedule,
    submit,
    retry,
    savedOnce: state.savedVersion > 0,
    /** 供浏览器/组件在离开或刷新前同步判断是否还有未落盘草稿。 */
    // 必须是箭头包装：直接导出原型方法会让调用方（如 CloudForm 存进 ref 后再调用）
    // 丢失 this，`this.state.phase` 抛 TypeError，刷新回填会被静默跳过。
    hasUnsaved: () => coordinator.hasUnsavedChanges(),
    /** 同步读取当前草稿版本，避免 React effect 更新滞后导致刷新回填竞态。 */
    getDraftRevision: () => coordinator.getState().draftVersion,
    getSavedRevision: () => coordinator.getState().savedVersion,
  }
}
