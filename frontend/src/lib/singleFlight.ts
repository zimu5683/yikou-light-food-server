/**
 * 单飞闸门：保证同一入口**任何时刻最多一个请求在途**。
 *
 * 为什么需要它：按钮的 `disabled={busy}` 只能拦住"下一次物理点击"——React 对离散事件
 * 会先刷新状态，所以人手双击通常会被 disabled 拦住；但同一个任务内的连点、或
 * `element.click()` 连续调用，会在 React 重新渲染之前把 handler 触发多次。
 * 上传/恢复这类**有一次性令牌或会产生审计写入**的入口必须有闸门，不能只靠 disabled。
 *
 * 另外每次成功 `begin()` 得到递增 token，`isCurrent(token)` 用来丢弃过期响应，
 * 防止更早请求的迟到响应覆盖更新的界面状态。
 *
 * 纯逻辑模块，Node 测试可直接覆盖（`singleFlight.test.ts`）。
 */
export class SingleFlightGate {
  private inFlight = false
  private seq = 0

  get pending(): boolean {
    return this.inFlight
  }

  /** 返回本次请求的 token；已有请求在途时返回 `null`（调用方必须直接 return）。 */
  begin(): number | null {
    if (this.inFlight) return null
    this.inFlight = true
    this.seq += 1
    return this.seq
  }

  finish(token: number): void {
    if (token === this.seq) this.inFlight = false
  }

  isCurrent(token: number): boolean {
    return token === this.seq
  }
}
