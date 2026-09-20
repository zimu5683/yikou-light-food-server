/**
 * 配置自动保存状态机与串行协调器。
 *
 * - 每次编辑递增 draftVersion；
 * - 同一时刻最多一个保存在途；在途期间的新编辑只标记 pending，不并发提交；
 * - 旧版本成功返回不会把更新的草稿标成“已保存”，而是立刻串行保存最新版本；
 * - 旧版本失败时保留最新草稿，停止自动队列，由用户重试保存最新版本；
 * - 保存失败/API 未就绪绝不显示 success。
 */

export type SavePhase = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'

export interface SaveState {
  phase: SavePhase
  /** 最近一次错误短句；phase=error 时非空。 */
  error?: string
  /** 最近一次成功保存的本地时间戳。 */
  savedAt?: number
  /** 当前草稿版本（仅用于展示/兼容旧调用）。 */
  draftVersion?: number
  /** 已成功落盘的最新版本。 */
  savedVersion?: number
  /** 正在保存的版本；0 表示没有在途请求。 */
  inFlightVersion?: number
  /** 在途期间是否又有新编辑等待合并保存。 */
  pending?: boolean
}

export type SaveEvent =
  | { type: 'change'; version?: number }
  | { type: 'submit'; at?: number; version?: number }
  | { type: 'success'; at: number; version?: number }
  | { type: 'failure'; error: string; version?: number }
  | { type: 'retry'; version?: number }

export const INITIAL_SAVE_STATE: SaveState = {
  phase: 'idle',
  draftVersion: 0,
  savedVersion: 0,
  inFlightVersion: 0,
  pending: false,
}

export function saveStateReducer(state: SaveState, event: SaveEvent): SaveState {
  const draftVersion = state.draftVersion ?? 0
  const savedVersion = state.savedVersion ?? 0
  const inFlightVersion = state.inFlightVersion ?? 0
  switch (event.type) {
    case 'change': {
      const nextDraft = event.version ?? draftVersion + 1
      if (state.phase === 'saving') {
        // 关键：保存中收到新改动必须记录新版本，不能把旧草稿当最终态。
        return { ...state, draftVersion: nextDraft, pending: true }
      }
      return { ...state, phase: 'dirty', draftVersion: nextDraft, pending: false, error: undefined }
    }
    case 'submit':
      return {
        ...state,
        phase: 'saving',
        inFlightVersion: event.version ?? draftVersion,
        pending: false,
        error: undefined,
      }
    case 'success': {
      const version = event.version ?? inFlightVersion
      if (event.version !== undefined && version < (state.draftVersion ?? 0)) {
        // 旧请求成功：只累计 savedVersion，不把当前草稿标记为已保存。
        return {
          ...state,
          savedVersion: Math.max(savedVersion, version),
          savedAt: event.at,
          inFlightVersion: 0,
          pending: (state.draftVersion ?? 0) > version,
          phase: 'saving',
        }
      }
      return {
        ...state,
        phase: 'saved',
        savedVersion: Math.max(savedVersion, version),
        savedAt: event.at,
        inFlightVersion: 0,
        pending: false,
        error: undefined,
      }
    }
    case 'failure':
      if (event.version !== undefined && event.version < draftVersion) {
        // 旧请求失败；更新的草稿仍需保留，状态退回未保存，让队列/用户重试最新值。
        return { ...state, phase: 'dirty', error: event.error, inFlightVersion: 0, pending: false }
      }
      return { ...state, phase: 'error', error: event.error, inFlightVersion: 0, pending: false }
    case 'retry':
      return { ...state, phase: 'saving', error: undefined, inFlightVersion: event.version ?? draftVersion, pending: false }
  }
}

export interface ConfigSaveResult {
  ok: boolean
  reason?: string
  message?: string
}

export type SaveRunner = () => Promise<ConfigSaveResult>

export interface SaveCoordinatorState extends Required<Pick<SaveState,
  'phase' | 'draftVersion' | 'savedVersion' | 'inFlightVersion' | 'pending'>> {
  error?: string
  savedAt?: number
}

/**
 * 版本化串行保存协调器（纯逻辑，可被 hook/浏览器测试直接驱动）。
 *
 * 不变式：
 * 1. running 时最多一个 saveRunner 在途；
 * 2. markEdited 在 running 中只增加 draftVersion 并置 pending；
 * 3. 在途成功若发现 draftVersion 更新，不进入 saved，继续串行保存最新版本；
 * 4. 在途失败保留最新草稿并停下，等待显式 retry。
 */
export class SaveCoordinator {
  private state: SaveCoordinatorState = {
    phase: 'idle', draftVersion: 0, savedVersion: 0,
    inFlightVersion: 0, pending: false,
  }
  private listeners = new Set<() => void>()
  private running = false
  private runner: SaveRunner | null = null

  getState = (): SaveCoordinatorState => this.state

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private emit(): void {
    for (const listener of this.listeners) listener()
  }

  private setState(patch: Partial<SaveCoordinatorState>): void {
    this.state = { ...this.state, ...patch }
    this.emit()
  }

  markEdited(): void {
    const nextDraft = this.state.draftVersion + 1
    if (this.running) {
      this.setState({ draftVersion: nextDraft, pending: true, phase: 'saving', error: undefined })
      return
    }
    this.setState({ draftVersion: nextDraft, pending: false, phase: 'dirty', error: undefined })
  }

  hasUnsavedChanges(): boolean {
    return this.state.phase === 'dirty' || this.state.phase === 'saving'
      || this.state.phase === 'error' || this.state.pending
  }

  /** 运行保存；若已有保存在途，只合并为 pending，返回 false，绝不并发。 */
  async run(runner: SaveRunner): Promise<boolean> {
    this.runner = runner
    if (this.running) {
      // 同一版本重复触发（连续点击立即保存/定时器晚到）不制造第二次提交；
      // 只有保存中确实产生了更新草稿才标记 pending。
      if (this.state.draftVersion > this.state.inFlightVersion) {
        this.setState({ pending: true, phase: 'saving' })
      }
      return false
    }
    // 已保存且没有新草稿时，显式“立即保存”不重复请求。
    if (this.state.phase === 'saved'
        && this.state.draftVersion === this.state.savedVersion) {
      return true
    }
    return this.drain()
  }

  private async drain(): Promise<boolean> {
    if (!this.runner) return false
    this.running = true
    try {
      while (true) {
        const version = this.state.draftVersion
        this.setState({
          phase: 'saving',
          inFlightVersion: version,
          pending: false,
          error: undefined,
        })
        let result: ConfigSaveResult
        try {
          result = await this.runner()
        } catch (error) {
          result = { ok: false, message: error instanceof Error ? error.message : String(error) }
        }
        if (!result?.ok) {
          this.setState({
            phase: 'error',
            error: result?.message || result?.reason || '保存失败',
            inFlightVersion: 0,
            pending: false,
          })
          return false
        }
        const savedVersion = Math.max(this.state.savedVersion, version)
        this.setState({ savedVersion, savedAt: Date.now() })
        if (this.state.draftVersion === version && !this.state.pending) {
          this.setState({ phase: 'saved', inFlightVersion: 0, pending: false, error: undefined })
          return true
        }
        this.setState({ phase: 'saving', inFlightVersion: 0, pending: true })
      }
    } finally {
      this.running = false
    }
  }
}

export interface SaveStateView {
  label: string
  tone: 'muted' | 'progress' | 'success' | 'warn'
  canRetry: boolean
}

export function saveStateView(state: Pick<SaveState, 'phase' | 'error' | 'pending'>): SaveStateView {
  switch (state.phase) {
    case 'idle':
      return { label: '尚未改动', tone: 'muted', canRetry: false }
    case 'dirty':
      return { label: '未保存', tone: 'warn', canRetry: false }
    case 'saving':
      return {
        label: state.pending ? '保存中…（有新改动待保存）' : '保存中…',
        tone: 'progress',
        canRetry: false,
      }
    case 'saved':
      return { label: '已保存', tone: 'success', canRetry: false }
    case 'error':
      return {
        label: state.error ? `保存失败：${state.error}` : '保存失败',
        tone: 'warn',
        canRetry: true,
      }
  }
}
