/**
 * Node 内置测试运行器覆盖桥接 cursor/重放去重逻辑。
 *
 * 运行：pnpm test（Node 原生类型擦除，不需要额外测试依赖）。
 * 传输层现在是 HTTP，因此这里用假的 globalThis.fetch 顶替后端。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

test('replayed bridge events are deduplicated and cursor advances', async () => {
  const storage = new Map<string, string>()
  const delivered: Array<Record<string, unknown>> = []
  let drainCalls = 0
  const event = {
    event: 'log' as const,
    payload: { ts: '00:00:00', level: 'INFO' as const, msg: 'hello' },
    event_id: 'producer-1:1',
    sequence: 1,
    created_at: 1,
    timestamp: 1,
    droppable: true,
  }
  const state = {
    version: 'test',
    status: 'ready' as const,
    event_producer_id: 'producer-1',
    config: {
      target_url: '', phone_number: '', excel_path: '', order_date: '',
      order_count: null, split_ratio: 0.38, sss_url: '', sss_account: '',
      sss_excel_path: '', sss_order_source: 'wps' as const,
      sss_product_name: '', sss_common_address: '',
      sss_use_fixed_address: true, sss_fixed_lnt: 0, sss_fixed_lat: 0,
      sss_fixed_area_code: '', sss_fixed_address_detail: '',
      sss_dry_run: true,
    },
    passwords: { order: '', sss: '' },
  }

  function json(payload: unknown): Response {
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) } as Response
  }

  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    if (url === '/api/bridge_ready') return json(state)
    if (url === '/api/drain_events') {
      drainCalls += 1
      const args = JSON.parse(String(init?.body ?? '[]')) as [number?]
      const lastSequence = Number(args[0] ?? 0)
      return json({
        events: lastSequence < 1 ? [event] : [],
        producer_id: 'producer-1',
        latest_sequence: 1,
        acked_sequence: lastSequence,
        dropped_count: 0,
        first_available_sequence: 1,
      })
    }
    return json({})
  }) as typeof fetch

  globalThis.window = {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
    },
    location: { search: '' },
  } as unknown as Window & typeof globalThis

  const bridge = await import('./bridge.ts')
  await bridge.connectBridge()
  bridge.onBridgeEvent((message) => delivered.push(message as unknown as Record<string, unknown>))

  // 同一批事件被后端重复返回两次：第二次必须按 event_id 去重，cursor 前进。
  await bridge.pullBridgeEvents()
  await bridge.pullBridgeEvents()

  assert.equal(drainCalls, 2)
  assert.equal(delivered.length, 1)
  assert.equal(bridge.bridgeCursor().sequence, 1)
  assert.equal(bridge.bridgeCursor().producerId, 'producer-1')
})
