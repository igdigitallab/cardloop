// spec-093 — unit tests for the runtime store's pure logic (web/src/lib/runtimeStatus.ts):
// tag assignment, THE account chain, and which window the pill leads with.
//
// Same harness as src/tabs/quickKeys.test.ts: Node's built-in runner, no new dependency.
// The module imports React and the api client, so it is bundled first (esbuild ships with
// vite) rather than compiled file-by-file:
//
//   cd web
//   npx esbuild src/lib/runtimeStatus.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/runtime-status-test/runtimeStatus.test.cjs --log-level=warning
//   node --test /tmp/runtime-status-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildRuntimeRows, effectiveRuntimeKey, inheritedRuntimeKey, chatIsPinned, leadWindow,
  globalDefaultAccount, unusableAccounts, publishCurrentChat, claimCurrentChat, clearCurrentChat,
  getRuntimeSnapshot, serverNow, type CurrentChat,
} from './runtimeStatus'
import { runtimeStats } from '../components/RuntimeTag'
import type { AgentProviderInfo } from '../types'
import type { UsageLimits, UsageLimitRow } from '../api'

const NOW = 1_790_000_000

function win(utilization: number, resetsIn = 3600, status = 'allowed'): UsageLimitRow {
  return { status, resets_at: NOW + resetsIn, utilization, ts: NOW }
}

function claude(accounts: { id: string; label: string; active?: boolean; available?: boolean }[],
  backends: { id: string; label: string; available: boolean }[] = []): AgentProviderInfo {
  return {
    provider: 'claude', enabled: true, available: true, authenticated: true,
    models: [{ value: 'opus', label: 'Opus', default: true } as never],
    reasoning_levels: [], capabilities: {},
    accounts: accounts.map(a => ({ id: a.id, label: a.label, active: !!a.active,
      available: a.available ?? true })),
    backends: [{ id: '', label: 'Claude subscription', available: true, models: [] },
      ...backends.map(b => ({ ...b, models: [{ value: 'qwen', label: 'qwen' }] }))],
  }
}

const CODEX: AgentProviderInfo = {
  provider: 'codex', enabled: true, available: true, authenticated: true,
  models: [{ value: 'gpt', label: 'gpt' } as never], reasoning_levels: [], capabilities: {},
  accounts: [], backends: [],
}

const LIVE = [
  claude([{ id: 'main', label: 'Main', active: true }, { id: 'work', label: 'work' }],
    [{ id: 'ollama', label: 'Ollama (local)', available: false }]),
  CODEX,
]

test('tags: main=M, work=W, Codex=C, local Ollama=L', () => {
  const rows = buildRuntimeRows(LIVE, null)
  assert.deepEqual(rows.map(r => [r.key, r.tag]), [
    ['claude:main:', 'M'], ['claude:work:', 'W'], ['claude::ollama', 'L'], ['codex::', 'C'],
  ])
})

test('tags: an account clashing with a fixed tag widens; the fixed tag never moves', () => {
  const rows = buildRuntimeRows([claude([{ id: 'main', label: 'Main' }, { id: 'c2', label: 'client' }]), CODEX], null)
  const tag = (k: string) => rows.find(r => r.key === k)!.tag
  assert.equal(tag('codex::'), 'C')
  assert.equal(tag('claude:c2:'), 'Cl')
  assert.equal(tag('claude:main:'), 'M')
})

test('tags: two accounts with the same initial never share a tag', () => {
  const rows = buildRuntimeRows([claude([{ id: 'a', label: 'Main' }, { id: 'b', label: 'Max' }])], null)
  const tags = rows.map(r => r.tag)
  assert.equal(new Set(tags).size, tags.length)
})

test('chain: an unpinned chat follows the GLOBAL account, not a hardcoded main', () => {
  assert.equal(effectiveRuntimeKey({ provider: 'claude' }, {}, 'work'), 'claude:work:')
  assert.equal(inheritedRuntimeKey({}, 'work'), 'claude:work:')
})

test('chain: project pin outranks global; chat pin outranks project', () => {
  assert.equal(effectiveRuntimeKey({ provider: 'claude' }, { account: 'work' }, 'main'), 'claude:work:')
  assert.equal(effectiveRuntimeKey({ provider: 'claude', account: 'main' }, { account: 'work' }, 'work'), 'claude:main:')
})

test('chain: Codex is its own runtime; a project backend pin wins over the chat on Claude', () => {
  assert.equal(effectiveRuntimeKey({ provider: 'codex', account: 'main' }, {}, 'main'), 'codex::')
  assert.equal(effectiveRuntimeKey({ provider: 'claude', account: 'main' }, { backend: 'ollama' }, 'main'), 'claude::ollama')
  assert.equal(effectiveRuntimeKey({ provider: 'claude', backend: 'ollama' }, {}, 'main'), 'claude::ollama')
})

test('pinned: any own account/provider/backend is a pin; nothing is not', () => {
  assert.equal(chatIsPinned({ provider: 'claude' }), false)
  assert.equal(chatIsPinned({ provider: 'claude', account: null, backend: '' }), false)
  assert.equal(chatIsPinned({ provider: 'claude', account: 'main' }), true)
  assert.equal(chatIsPinned({ provider: 'codex' }), true)
  assert.equal(chatIsPinned({ provider: 'claude', backend: 'ollama' }), true)
})

function mainRow(limits: Record<string, UsageLimitRow>) {
  return buildRuntimeRows([claude([{ id: 'main', label: 'Main', active: true }])], {
    limits, now: NOW, account: 'main', accounts: null,
  })[0]
}

test('pill: always the 5-hour window — even when the weekly is higher', () => {
  assert.equal(leadWindow(mainRow({ five_hour: win(0.03), seven_day: win(0.18, 500_000) }), NOW)?.key, 'five_hour')
  assert.equal(leadWindow(mainRow({ five_hour: win(0.18), seven_day_fable: win(0.99, 500_000) }), NOW)?.key, 'five_hour')
})

test('pill: a SPENT weekly limit overrides the 5-hour window (the runtime is cut off)', () => {
  const row = mainRow({ five_hour: win(0.03), seven_day: win(1.0, 500_000, 'rejected') })
  assert.equal(leadWindow(row, NOW)?.key, 'seven_day')
  assert.equal(runtimeStats(row, NOW).cls, 'usage-red')
  // A spent per-model bucket does not — it only bites when that model runs.
  assert.equal(leadWindow(mainRow({ five_hour: win(0.03), seven_day_fable: win(1.0, 500_000, 'rejected') }), NOW)?.key, 'five_hour')
})

test('pill: a 5-hour window that already reset says nothing about now — no number', () => {
  const row = mainRow({ five_hour: win(0.99, -10), seven_day: win(0.2, 500_000) })
  assert.equal(leadWindow(row, NOW), null)
  assert.equal(runtimeStats(row, NOW).pct, '—')
})

test('limits: multi-account block feeds each row; an aged inactive reading is stale', () => {
  const usage: UsageLimits = {
    limits: { five_hour: win(0.22) }, now: NOW, account: 'main',
    accounts: [
      { id: 'main', label: 'Main', is_main: true, active: true, ok: true, reason: '', email: '',
        plan: 'max', shared_ok: true, shared_broken: [], limits: { five_hour: win(0.22) }, limits_ts: NOW - 30 },
      { id: 'work', label: 'work', is_main: false, active: false, ok: true, reason: '', email: '',
        plan: 'team', shared_ok: true, shared_broken: [], limits: { five_hour: win(0.98) }, limits_ts: NOW - 2000 },
    ],
  }
  const rows = buildRuntimeRows(LIVE, usage)
  const main = rows.find(r => r.key === 'claude:main:')!
  const work = rows.find(r => r.key === 'claude:work:')!
  assert.equal(main.windows?.five_hour.utilization, 0.22)
  assert.equal(main.stale, false)
  assert.equal(work.windows?.five_hour.utilization, 0.98)
  assert.equal(work.stale, true)
  assert.equal(rows.find(r => r.key === 'claude::ollama')!.hasQuota, false)
})

test('global default: usage wins, registry next, main last', () => {
  assert.equal(globalDefaultAccount(LIVE, { limits: {}, now: NOW, account: 'work' }), 'work')
  assert.equal(globalDefaultAccount(LIVE, null), 'main')
  assert.equal(globalDefaultAccount([], null), 'main')
})

test('chain: a project pinned to a logged-out account shows the global one (server degrades)', () => {
  const providers = [claude([{ id: 'main', label: 'Main', active: true },
    { id: 'work', label: 'work', available: false }])]
  const bad = unusableAccounts(providers)
  assert.deepEqual([...bad], ['work'])
  assert.equal(effectiveRuntimeKey({ provider: 'claude' }, { account: 'work' }, 'main', bad), 'claude:main:')
  assert.equal(inheritedRuntimeKey({ account: 'work' }, 'main', bad), 'claude:main:')
  // A CHAT pin is strict server-side (the turn is refused) — it is shown as-is, not masked.
  assert.equal(effectiveRuntimeKey({ provider: 'claude', account: 'work' }, {}, 'main', bad), 'claude:work:')
})

test('stats: an empty Codex WALLET never leads — it is reported "rejected" on healthy plans too', () => {
  const rows = buildRuntimeRows([CODEX], {
    limits: {}, now: NOW, account: 'main',
    codex: { ts: NOW - 60, plan_type: 'plus', limit_name: null, limits: {
      primary: win(0.4), credits: { status: 'rejected', resets_at: null, utilization: null, ts: NOW },
    } },
  })
  assert.equal(leadWindow(rows[0], NOW)?.key, 'primary')
  const s = runtimeStats(rows[0], NOW)
  assert.equal(s.pct, '40%')
  assert.equal(s.cls, 'usage-green')
  // …but a rate-limited primary window (the real block) does lead, red.
  const blocked = buildRuntimeRows([CODEX], {
    limits: {}, now: NOW, account: 'main',
    codex: { ts: NOW - 60, plan_type: 'plus', limit_name: null, limits: {
      primary: win(1.0, 600, 'rejected'), secondary: win(0.2, 90_000) } },
  })
  assert.equal(runtimeStats(blocked[0], NOW).cls, 'usage-red')
})

test('limits: a single-account install with an unnamed active account keeps its numbers', () => {
  const [row] = buildRuntimeRows([claude([{ id: 'main', label: 'Main', active: true }])], {
    limits: { five_hour: win(0.33) }, now: NOW, accounts: null,
  })
  assert.equal(row.windows?.five_hour.utilization, 0.33)
})

test('reason: a row disabled by its PROVIDER says so (not "needs a login")', () => {
  const off = { ...CODEX, enabled: false }
  assert.match(buildRuntimeRows([off], null)[0].reason || '', /switched off/)
  const down = { ...claude([{ id: 'main', label: 'Main', active: true }]), available: false, error: 'CLI missing' }
  assert.equal(buildRuntimeRows([down], null)[0].reason, 'CLI missing')
})

test('rows: a duplicated key renders once with one tag', () => {
  const rows = buildRuntimeRows([claude([{ id: 'main', label: 'Main' }, { id: 'main', label: 'Main' }])], null)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].tag, 'M')
})

test('stats: allowed_warning is yellow; a stale reading is dimmed with a star', () => {
  const [row] = buildRuntimeRows([claude([{ id: 'main', label: 'Main', active: true }])], {
    limits: { five_hour: win(0.3, 3600, 'allowed_warning') }, now: NOW, account: 'main', accounts: null,
  })
  assert.equal(runtimeStats(row, NOW).cls, 'usage-yellow')
  const s = runtimeStats({ ...row, stale: true }, NOW)
  assert.equal(s.pct, '30%*')
  assert.equal(s.cls, 'usage-dim')
  assert.equal(runtimeStats(row, NOW).reset, '1h 0m')
  assert.equal(runtimeStats(row, NOW, true).reset, '1h')
})

test('stats: a local backend has no quota — "local" / "off", never a percentage', () => {
  const rows = buildRuntimeRows(LIVE, null)
  const l = rows.find(r => r.key === 'claude::ollama')!
  assert.equal(runtimeStats(l, NOW).pct, 'off')
  assert.equal(runtimeStats({ ...l, available: true }, NOW).pct, 'local')
})

function cur(owner: string, runtimeKey: string): CurrentChat {
  return { owner, projectName: owner, chatName: 'Main', runtimeKey, inheritedKey: 'claude:main:',
    pinned: false, projectAccount: null, projectBackend: '', busy: false, error: '',
    pick: () => {}, followDefault: () => {} }
}

test('split: the pill follows the pane last TOUCHED, and a data update never steals it', () => {
  const owner = () => getRuntimeSnapshot().current?.owner ?? null
  const seen: (string | null)[] = []
  publishCurrentChat(cur('left', 'claude:main:'));                    seen.push(owner())
  publishCurrentChat(cur('right', 'codex::'));                        seen.push(owner()) // mounted 2nd
  publishCurrentChat({ ...cur('right', 'codex::'), busy: true });     seen.push(owner()) // streams
  claimCurrentChat('right');                                          seen.push(owner()) // clicked in
  publishCurrentChat(cur('left', 'claude:work:'));                    seen.push(owner()) // left updates
  assert.equal(getRuntimeSnapshot().current?.runtimeKey, 'codex::')
  clearCurrentChat('right');                                          seen.push(owner()) // right closed
  assert.equal(getRuntimeSnapshot().current?.runtimeKey, 'claude:work:', 'fallback shows the latest data')
  clearCurrentChat('left');                                           seen.push(owner())
  assert.deepEqual(seen, ['left', 'left', 'left', 'right', 'right', 'left', null])
})

test('publish: an identical payload with a new closure does not replace the slot', () => {
  const a = cur('solo', 'claude:main:')
  publishCurrentChat(a)
  const before = getRuntimeSnapshot().current
  publishCurrentChat({ ...a, pick: () => {}, followDefault: () => {} })
  assert.equal(getRuntimeSnapshot().current, before)
  clearCurrentChat('solo')
  assert.equal(getRuntimeSnapshot().current, null)
})

test('clock: serverNow advances the frozen server time by local elapsed time', () => {
  const realNow = Date.now
  try {
    Date.now = () => 1_000_000 * 1000
    const t = serverNow({ usage: { limits: {}, now: 5000, account: 'main' }, usageAt: 1_000_000 - 90 })
    assert.equal(Math.round(t), 5090)
  } finally {
    Date.now = realNow
  }
})
