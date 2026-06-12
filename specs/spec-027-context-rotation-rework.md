---
created: 2026-06-11
updated: 2026-06-12
status: draft
phases_shipped: none
card: ops:spec027
supersedes_behavior: spec-021 (auto-rotation)
pairs_with: spec-028 (Track 2 — persistent client)
---

# Spec 027 — Context Cost Reduction (Track 1: interim + permanent wins)

## Goal

Cut ClaudeOps token burn toward terminal-CLI levels **without** the big architecture change.
Four independent levers: (1) stop the self-inflicted burn from the spec-021 auto-rotation,
(2) make the rare manual rotation cheap, (3) shrink the per-turn baseline by putting every
CLAUDE.md on a diet (permanent win, biggest lever = the new-project template), (4) clean up
the cockpit context UI (kill a duplicate, make manual wrap the primary cue). The
architectural fix that truly matches the CLI — native auto-compact via a long-lived client —
is **spec-028 (Track 2)** and lands separately.

---

## Context / Motivation (measured; corrected after adversarial double-check)

`claude-agent-sdk` 0.2.96, real transcripts. The first draft of this spec made two wrong
claims — corrected here.

### The fixed per-turn baseline (~30–42K)

Every turn loads a static system-prompt prefix: `claude_code` preset + all tool schemas
(~17–18K) + project CLAUDE.md (~5.8K) + parent-dir `~/CLAUDE.md` (~3.2K) + MEMORY.md index
(~2.6K). On a **cold** start (fresh session or after the prompt-cache TTL expires) the whole
prefix is paid as expensive `cache_creation`.

- **CORRECTION 1 (double-load REFUTED):** `~/CLAUDE.md` is **not** loaded twice. The `user`
  setting source = `~/.claude/settings.json`, **not** `~/CLAUDE.md`. `~/CLAUDE.md` is loaded
  once via the CLI walking up parent directories. Earlier "loaded twice when cwd=~" was wrong.

### Caching within a warm session is HEALTHY (91–99%)

Measured 96–100% cache-hit from turn 3 onward, holding past 100K (one CLI session held 100%
to 579K across 4 native compactions). Cross-session prefix caching **already works**: a fresh
session 2.5 min after a prior one hit ~75% on the baseline.

- **CORRECTION 2 (`exclude_dynamic_sections` downgraded):** it strips only cwd/git/MEMORY.md
  from the cached prefix and re-injects them into the first user message. CLAUDE.md (via
  `setting_sources`) is already in the static cacheable prefix, so this option is **not** the
  cross-session baseline fix the draft treated it as. Net benefit: ~1.3K smaller system
  prompt + eliminates the edge case where a MEMORY.md edit breaks the prefix. Keep it as
  cheap hardening, not a headline.

### Cache reality (confirmed from transcripts) — frames everything below

- Auth is **subscription/OAuth**, so "cost" = **rate-limit consumption**, not dollars.
- The CLI already uses Anthropic's **1-hour extended cache TTL** (every write lands in
  `ephemeral_1h_input_tokens`; zero in the 5-min bucket). This is CLI-internal — **cannot be
  extended further (1h is the max) or turned off**. The cockpit's `CACHE_TTL_MIN=60` is
  therefore correct.
- **Cache reads refresh the TTL for free.** So any gap **< 1 hour** re-warms at read price
  (0.1×) — effectively free. Only gaps **> 1 hour** (e.g. overnight) force a cold re-write.
- A cold cache **write costs 2× base** (1h-TTL write multiplier). So every cold start is
  doubly expensive → **shrinking the baseline (Part 4) is the highest-value structural lever**.
- The cache is **server-side**: the `query()`-per-turn vs persistent-client process model does
  NOT change cache behaviour. Track 2's value is auto-compact + latency, not cache.

### So where does the burn actually come from? (ranked, rate-limit terms)

1. **Cold-start 2× writes.** Every fresh baseline write costs 2×: fresh session, our
   rotation-clear, `/reset`, restart, model-switch, or a >1h idle gap. The biggest avoidable
   source. → Part 4 (smaller baseline) + Part 1 (stop frequent clears).
2. **The spec-021 auto-rotation itself.** Fired at 60K — just above the ~42K floor → freed
   ~18K but forced a 2× cold re-write on the next turn. Net negative. → Part 1.
3. **Haiku summarizer double-pays ~60K.** Resumes the full transcript with haiku (cold for
   haiku) just to write ≤500 words. → Part 2.
4. **`/model` switch mid-session.** Cache is per-model; one switch at 110K cost ~90–107K cold
   write (16% hit vs 97%). Real fix = per-session pinning (Track 2).
5. **>1h idle gaps** (overnight). Minor on subscription; a keep-alive ping could refresh the
   TTL but it's only rate-limit, not dollars — **not worth the complexity.** Accept it.

### Window-cost reality — REORDERS the priorities above (research 2026-06-12)

The 5-hour window meters **cost-weighted units**, not raw tokens (strong inference from API
price ratios + the observed "Fable = 2× Opus" behaviour; Anthropic does not publish the exact
formula — verify empirically: run identical opus vs sonnet tasks, compare `utilization` delta):

- **output incl. thinking = ~5×** · cache-write = 2× · fresh input = 1× · cache-read = 0.1×.
- **Thinking is the biggest hidden burn.** Extended thinking is on by default and bills as
  output (5×); a request can emit tens of thousands of thinking tokens. **On Fable 5 thinking
  cannot be disabled** and runs at high effort. On opus/sonnet it's controllable (effort level).
- **Model weight:** Fable ~3.3× sonnet, Opus ~1.7×, Haiku ~0.33×. Default is already `opus`
  (good — the biggest model lever is pulled); fable-conductor is triple-costly.
- **⚠️ Fable free on subscription only through ~2026-06-22**, then usage-credits at API rates
  (web-research finding, MEDIUM confidence — verify). Keep `DEFAULT_MODEL=opus`; use fable
  deliberately and sparingly, especially after that date.
- **Felt-lever re-ranking:** (1) thinking/effort control, (2) model choice (already opus),
  (3) subagent fan-out (Part 7), (4) fewer cold starts / no 60K rotation (Part 1), (5) baseline
  diet (Part 4), (6) output verbosity. The diet is permanent hygiene but NOT the top felt lever
  — thinking/output and orchestration are.

---

## Design — Part 1: Remove the 60K auto-rotation, keep a HIGH safety backstop

- **Delete the aggressive auto-trigger** at `webapp.py:6368-6391` and the
  `_maybe_rotate_tg` call at `bot.py:1064`. The 60K threshold is the bug — it rotated
  constantly for almost no headroom.
- **High safety backstop @ 175K (DECIDED 2026-06-12).** Removing *all* automatic protection
  is risky in the `query()`-per-turn model: with no native compaction a session can grow to
  the **200K hard wall and the next turn errors out mid-task**. So keep a **single high
  backstop** — rotate once at **175K** (`CONTEXT_ROTATE_AT` default raised 60000 → 175000):
  rare, only near the wall, never the mid-task surprise at 60K. **Interim only — spec-028's
  native auto-compact removes the backstop entirely.**
- **Keep intact:** `_do_session_rotation`, manual endpoint `api_project_rotate`
  (`webapp.py:6474`), handoff file write, and `pending_handoff` injection into the next fresh
  turn. Manual "♻ Wrap & reset" stays fully functional.
- `CONTEXT_ROTATION` env → kill-switch for the manual path + the backstop.

## Design — Part 2: Make the (now rare) rotation summary cheap

- `_do_session_rotation` (`webapp.py:~3619-3631`) must stop passing
  `resume_session_id=resume_sid` to the haiku call. Instead read the **tail** of the session
  jsonl (~last 10–15 events) and inline a condensed digest into `ROTATION_SUMMARY_PROMPT`.
  Target ~≤10K vs ~60K.
- **Evaluate `fork_session=True`** (`types.py:1790`) as an alternative to clear-and-cold-start:
  it forks a new session id that inherits the prior context, potentially preserving more cache
  continuity than our summarize-and-clear. Spike during implementation.

## Design — Part 3: `exclude_dynamic_sections=True` (cheap hardening, not headline)

- Add `"exclude_dynamic_sections": True` to the preset dict (`bot.py:625`, options at
  `bot.py:642-650`, and the secondary preset at `bot.py:927`).
- **Corrected acceptance:** verify turn-1 `input_tokens` drops by ~the MEMORY.md token count
  (memory moved out of the system prompt), and the agent still sees cwd/git/memory. Do NOT
  claim it as the cross-session baseline fix (that already works).
- **Never drop the `user` setting source to shrink the baseline** — the
  `guard-self-lifecycle.sh` safety hook lives there.

## Design — Part 4: CLAUDE.md diet (permanent win; biggest lever = the template)

CLAUDE.md rides the static prefix every turn; smaller + more stable = cheaper cold starts and
less cache invalidation. **Plan, do not execute until approved.**

**Diet rubric (reusable):** Keep in CLAUDE.md only what the agent needs on turn 1 *before it
knows the task* — routing rules, destructive-op/credential guards, always-used constants, and
gotchas where not-knowing causes an irreversible mistake. Move everything else to
reactively-read files (`ARCHITECTURE.md`, a new `GOTCHAS.md`, vault, or `templates/reference/`).
Separate **volatile** facts (UUIDs, accumulating gotchas, infra topology) out of the cached
prefix so routine edits don't invalidate it.

| File | Now | Target | Action |
|------|-----|--------|--------|
| `templates/CLAUDE.md.tpl` (**every new project**) | ~3.5K | ~1.1K (-69%) | Move the 150-line error-handler code blocks → `templates/reference/error-handler.md`; keep lean stubs + the "cockpit rules" section. **Highest leverage — do first.** |
| `~/CLAUDE.md` (rides every project) | ~3.2K | ~2.0K (-38%) | Move VM table → Homelab/, CF zone IDs → Cloudflare.md, Coolify project/env UUIDs → new `~/vault/_system/Coolify.md`, TickTick project IDs → Credentials.md. Trim role/quick-rules. Stabilize. |
| `claude-ops-bot/CLAUDE.md` | ~5.8K | ~2.4K (-59%) | Create `GOTCHAS.md`; move the subsystem gotcha catalogue (TG-render detail, concurrency, security, C2-worktree, memory/secrets internals, timeline) there; keep turn-1 safety gotchas. |
| Other project CLAUDE.md (networking-os, etc.) | varies | ~-50% | Opportunistic: strip template-boilerplate blocks (Секреты/Память/Кокпит now reference-only), move subsystem detail → per-project ARCHITECTURE.md. |

Cold-start saving ≈ **51%** for a claude-ops-bot session (9.0K → 4.4K) and ~54% per new
project. Mitigation against losing gotchas: relocate into `GOTCHAS.md`/reference, **never
delete**; add a one-line pointer in the CLAUDE.md header.

## Design — Part 5: Cockpit context UI — kill the duplicate, one session-health row

The same context number renders in **two** places with **conflicting** thresholds:
- Location 1 `chat-stats-inline` (`ChatTab.tsx:656`): `💬N · ~52K`, yellow@120K/red@200K.
- Location 2 `chat-ctx-indicator` (`ChatTab.tsx:680-733`): `52K`, yellow@40K/red@60K, hides
  the `♻ Wrap & reset` button inside it (only visible once red).

- **Remove Location 2.** Expand Location 1 into one always-visible **session-health row**:
  message count · mini progress bar (fill = tokens/200K, one consistent color scale) · token
  count · cache countdown (`♨️ MM:SS`/`⚪ cold`) · **always-present** `♻ Wrap & reset` (muted
  <40K, prominent >120K) · optional `⏱ utilization%` (the `result` event already sends
  `utilization` — add it to `TurnMetrics` in `types.ts` and render it).
- Align thresholds to the manual-wrap world: yellow@~120K (advisory), red@~160–175K (wrap now).
- **Cache indicator (`ChatTab.tsx:60-62,736-757`):** the `CACHE_TTL_MIN=60` constant is
  **correct** (confirmed: CLI uses 1h TTL). But the header countdown is a pure client timer,
  blind to non-timer cache resets (rotation, `/reset`, restart, model-switch all kill the
  server cache independently). Fix: additionally derive warm/cold from the **last turn's real
  `cache_hit_pct`** (already on `msg.metrics`, trustworthy) so it can't show "warm" after a
  silent reset; keep the per-turn `cache X%` footer as the ground-truth signal.
- **Quick wins** (low-effort): dead Tailwind classes (`text-red-500` etc. — project has no
  Tailwind; inline style wins) → remove; duplicate timestamp in per-turn footer
  (`ChatTab.tsx:835`) → drop; cache `setInterval` runs every second even when idle
  (`ChatTab.tsx:294`) → guard on `lastTurnEndMs`; clarify `↺` (new, no summary) vs `♻` (wrap +
  summary) tooltips; rename `SessionContextPanel` "Context: N files" → "Touched: N files".

## Design — Part 6: Thinking / effort control (the top felt-window lever)

Output incl. thinking weighs ~5× in the window; thinking is on by default. ClaudeOps sets no
thinking/effort config (`bot.py:642-650` has no `thinking`/`effort`). Levers:
- **Set a routine effort level** for opus production ops (e.g. `medium`), reserving `high`/`xhigh`
  for deliberately hard tasks. Via `effort: EffortLevel` on options / per `AgentDefinition`.
- **`thinking: ThinkingConfig` = disabled** for `haiku`/`quick` subagents (they don't need it) —
  cuts their output-weighted burn + latency. (Deprecated `max_thinking_tokens` → use `thinking`.)
- **Fable caveat:** thinking is NOT disable-able on Fable 5 and runs high — a strong reason to
  keep fable off the default and use it sparingly (compounds with the ~2026-06-22 billing change).
- Optional: surface a per-turn thinking-token count in the cockpit (currently invisible; pairs
  with spec-022 cost fields).

## Design — Part 7: Lean subagents (kills the orchestration multiplier)

Each subagent pays its OWN cold baseline (~20K, dominated by the ~17.5K tool preset) — NOT shared
with the conductor's cache. A typical fable task fans out ~6.6 subagents = ~4.8× a single session;
heavy audits hit 31×. Mitigations (only bite when fable conducts; opus-default already runs inline):
- **Minimal tool set per subagent** via `AgentDefinition.tools` — e.g. `researcher`/`quick` get
  `["Bash","Read","Grep","Glob"]` instead of the full preset → baseline ~18K → ~3–5K each.
  **Biggest orchestration lever.** Verify the trimmed set still lets each role work.
- **Cap fan-out:** `AgentDefinition.maxTurns` per role + a "≤3–5 subagents, sequence don't
  needlessly parallelize" line in `CONDUCTOR_PROMPT` — prevents 51-agent cascades.
- **Lean subagent prompt:** consider `setting_sources=[]` / `skills=[]` so subagents skip CLAUDE.md
  (spike — `AgentDefinition` may not expose `setting_sources`).
- The CLAUDE.md diet (Part 4) multiplies here: smaller baseline × N subagents.

## Design — Part 8: Tool-overhead trim (small, optional — door mostly closed)

The ~17–18K preset+tools is ~85% fixed (compiled into the CLI binary). Realistic trim ~2.2K:
a DISALLOWED_TOOLS spike (add ~5 never-used tools, measure if `cache_creation` drops) + disable
the `everything-evenhub` plugin for ops-bot (`.claude/settings.json`). Smaller than the diet; do
only if the spike shows schema exclusion is real.

---

## Phases

| Phase | Part | Description | Status |
|-------|------|-------------|--------|
| 1 | 1 | Remove 60K auto-trigger (web+TG); set high backstop (~175K) per decision | planned |
| 2 | 2 | Cheap summary (no full-transcript resume) + evaluate `fork_session` | planned |
| 3 | 3 | `exclude_dynamic_sections=True` + corrected verification | planned |
| 4 | 4 | CLAUDE.md diet — **template first**, then `~/CLAUDE.md`, then project, then others | planned |
| 5 | 5 | Cockpit UI: remove duplicate, one session-health row + quick wins | planned |
| 6 | 6 | Thinking/effort: routine `medium` for opus, `thinking=disabled` for haiku subagents | planned |
| 7 | 7 | Lean subagents: minimal `tools` per role + fan-out cap in CONDUCTOR_PROMPT | planned |
| 8 | 8 | (optional) DISALLOWED_TOOLS spike + disable evenhub plugin for ops-bot | optional |

Phases are independent; ship in any order. **Felt-window priority:** Part 6 (thinking) and
Part 7 (lean subagents) are the top levers the operator will actually notice; Part 1 (no 60K
rotation) and Part 4 (diet) are permanent hygiene. Part 8 is marginal.

---

## Acceptance

- [ ] Normal turns at 60–150K trigger **no** rotation; only the ~175K backstop rotates once
      (or none, if decision = true-zero).
- [ ] Manual `POST /rotate` still summarises → handoff file → injects once on next fresh turn;
      its summary run consumes ~≤10K (not ~60K).
- [ ] `exclude_dynamic_sections`: turn-1 `input_tokens` drops ~MEMORY.md size; agent still has
      cwd/git/memory awareness.
- [ ] Diet: template, `~/CLAUDE.md`, `claude-ops-bot/CLAUDE.md` hit token targets; all moved
      gotchas live in `GOTCHAS.md`/reference (nothing deleted); a fresh claude-ops-bot session
      shows ≈half the baseline `cache_creation`.
- [ ] Cockpit shows the context number in exactly **one** place; `♻ Wrap & reset` is always
      reachable; `utilization` is visible.

---

## Tests

- `tests/test_context_rotation.py`: invert auto-trigger tests → "no auto-rotation below the
  backstop"; add a backstop test at ~175K; keep all manual-path tests; assert the summary call
  uses no full-transcript resume; assert options carry `exclude_dynamic_sections=True`.
- Full suite green via `venv/bin/python -m pytest tests/` (≈950). Frontend: `cd web && npm run build`.

---

## Implementation notes & harvested agent ideas (2026-06-12, post Parts 1/3/4/5/6/7)

Parts 1, 3, 4, 5, 6, 7 shipped to the working tree; full suite green (**1067 passed / 6 skipped**).
SDK 0.2.96 fields confirmed from the installed source: `SystemPromptPreset.exclude_dynamic_sections`,
`ClaudeAgentOptions.effort` (`EffortLevel = low|medium|high|xhigh|max`), `ClaudeAgentOptions.thinking`
(`ThinkingConfigDisabled`), `AgentDefinition.tools|effort|maxTurns`. **`AgentDefinition` has NO
`thinking` field** → per-subagent thinking-disable is impossible; used `effort="low"` on the
quick/haiku role as the closest lever. Opus effort gated behind env `DEFAULT_EFFORT` (default
`medium`, tunable + reversible — no per-task escalation mechanism exists yet, hence the env gate).

### Top remaining lever — early-warn at ~150K (closes the ORIGINAL mid-task pain)
The 175K backstop checks only on the `result` event = AFTER the turn → it CANNOT catch a single fat
turn that jumps ~140K→200K within one turn (structural blind spot; it only catches slow multi-turn
growth). Cheap mitigation, no engine surgery: on the same `result` event, when `context_tokens >
CONTEXT_WARN_AT` (~150K) emit `{type:context_warn}` → SSE + TG push so the operator can `/rotate` or
shrink the next request before the fat turn. Prophylaxis for turn N+1; true fix for "jump within turn
N" = pre-flight prompt-size estimate before `run_engine` (separate, costlier spec). **Recommended as
the next thing to build — it directly addresses the surprise that started this whole spec.**

### Part 2 refinement (cheap rotation summary)
175K is now the ONLY case rotation fires, so its cost matters more. `_do_session_rotation` still
resumes the FULL transcript with haiku to write ≤500 words — pays ~175K prefill exactly when fighting
for tokens. Fix direction: fresh haiku session (`resume_session_id=None`) + inline the jsonl TAIL
(~last 10–15 events / N KB) into the prompt. Recon first: Agent-SDK session jsonl path + schema
stability before relying on it.

### Part 5 UI follow-ups (harvested)
- Tooltip hardcodes "Base floor ~11–14K" — WRONG (real floor ~30–42K, shifts after each diet). Remove
  the number; keep qualitative text. [trivial — doing now]
- Add per-turn token delta `+NK` to the health row (growth rate beats absolute). [small — doing now]
- **Bus-path metrics gap:** card/TG turns finalize via `finalizeStreaming` (no metrics) → utilization /
  cache% / per-turn footer NEVER appear for card/TG runs → operator sees "metrics sometimes missing".
  Real fix = thread metrics through the bus path. [medium — separate]
- Minor: `estimateTokens` badly underestimates on fresh resume until the first `result` (only the `~`
  distinguishes rough vs real); `showColdDivider` duplicates the cache indicator; 4 overlapping
  duration formatters — consolidation candidates.

### Part 4 diet — GOTCHAS discoverability (the residual risk)
A header pointer is weak — an agent editing one subsystem may skim past it and repeat a fixed bug.
Escalating fixes: (cheap) inline `(⚠ gotchas → GOTCHAS.md)` markers next to bug-prone subsystems in
the "what's where" map; (medium) anchor-TOC atop GOTCHAS.md so one Grep lands the block; (strong,
closes the class) move each fixed-bug gotcha into a CODE COMMENT next to the code (`# GOTCHA: … see
GOTCHAS.md`) — the agent always opens the function it edits, may not open the doc. CLAUDE.md/GOTCHAS
stays the home for cross-file gotchas; per-function ones belong at the code. [separate code task]
Hygiene (not cache weight): DONE.md (~21.9K) + CHANGELOG.md (~10.2K) are the biggest root docs but are
NOT read per turn — periodic truncation, not diet.

### Audit smells (engine + sdkconfig, not urgent)
- `ctx_tokens = event.get("context_tokens", 0)` silently 0 if the engine omits the field → rotation &
  warn never fire = silent drive into the wall. Log once if absent (cheap contract-regression catch).
- Chat `_QUEUE` vs TG `_TG_QUEUE` rotation guards are asymmetric (cwd-lock partly covers). Audit.
- `_rotated_this_turn` is functionally dead in the chat path (harmless). `_notify_tg_rotation`
  duplicates the TG-notify path — unify candidate.
- `_build_agents_kwargs` clones `AgentDefinition` field-by-field → silently drops any NEW SDK field on
  a future bump → use `dataclasses.replace(agent_def, model=…)`. Conductor itself has no `max_turns`
  cap (looping fable conductor unbounded → consider env `MAX_CONDUCTOR_TURNS`). A caller passing its
  own `system_prompt` dict to `run_engine` bypasses `exclude_dynamic_sections` (future footgun).

## Risks

- **No native compaction until Track 2.** The ~175K backstop is the only automatic guard;
  below it, long sessions rely on the operator + the UI cue. Caching keeps them cheap; quality
  drift past ~120K is operator-controlled (the point). Cache-TTL re-creation on idle gaps is
  unaffected by this spec — only Track 2 (warm persistent process) addresses it.
- **Diet could hide a gotcha.** Relocate to `GOTCHAS.md`/reference, never delete; header
  pointer so the agent fetches it.
- **`exclude_dynamic_sections` changes prompt structure** — gated by the Part 3 verification.

## Non-goals

- Long-lived `ClaudeSDKClient` / native auto-compact → spec-028.
- Per-session model pinning (the `/model` cache cost) → spec-028.
- Rotation-history UI, per-card memory accumulation (unchanged from spec-021).

## Related

- Spec 028 — Persistent client / native auto-compact (the real CLI-parity fix; deletes this
  spec's rotation machinery, keeps the diet).
- Spec 021 — Context rotation (this removes its 60K auto-trigger; keeps manual + indicator).
- Spec 020 — Deferred runs · Spec 017 — Fable orchestrator (model-switch cost).
