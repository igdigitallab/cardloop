# Research: addyosmani/agent-skills as Default Playbook Source

> Status: COMPLETE  
> Date: 2026-06-11  
> Source: https://github.com/addyosmani/agent-skills (MIT License, Addy Osmani et al.)  
> Version evaluated: v0.6.2  
> Verdict counts: DUPLICATE 10 · ADAPT 8 · SKIP 6

Attribution: All skills evaluated below are MIT-licensed.
See https://github.com/addyosmani/agent-skills/blob/main/LICENSE

---

## Summary

The repo packages 24 engineering-workflow skills across six phases (Define → Plan → Build →
Verify → Review → Ship). For ClaudeOps the relevant question is: which of these should
become default executor playbooks / prompt templates so that OSS users get a senior-engineer
workflow out of the box?

**Short answer:** 10 skills duplicate what the ClaudeOps engine already provides (quality gate,
conductor, our existing code-reviewer / test-master / security-review). 8 skills contain
patterns worth adapting into `data/prompts.json` defaults or executor prompt text. 6 can
be skipped outright (browser-specific stacks, CI infra, or content too broad for a general
executor preset).

---

## Full Verdict Table

| Skill | Phase | Verdict | One-line rationale |
|---|---|---|---|
| `using-agent-skills` | Meta | DUPLICATE | Routing/meta — ClaudeOps conductor (spec-017) does this |
| `interview-me` | Define | ADAPT | Best gap-finder before a spec; see §Personal Install |
| `idea-refine` | Define | ADAPT | Structured diverge→converge before spec; see §Personal Install |
| `spec-driven-development` | Define | ADAPT | Assumption-surfacing + gated workflow; core of any OSS preset |
| `planning-and-task-breakdown` | Plan | ADAPT | Dependency-graph decomposition; fits executor pre-run phase |
| `doubt-driven-development` | Plan | ADAPT | Adversarial self-review before commit; good for executor gate |
| `incremental-implementation` | Build | DUPLICATE | Board auto-run + worktree cycle already enforces slice-by-slice |
| `context-engineering` | Build | DUPLICATE | CLAUDE.md hierarchy already defined in spec-017 conductor |
| `source-driven-development` | Build | ADAPT | Detect stack → fetch official docs → cite; valuable for executor |
| `api-and-interface-design` | Build | SKIP | Hyrum's Law and one-version rule — too wide for a compact preset |
| `frontend-ui-engineering` | Build | SKIP | React/Tailwind specifics — not generic enough for OSS executor |
| `debugging-and-error-recovery` | Verify | ADAPT | Stop-the-line + triage checklist; adapt into executor error path |
| `test-driven-development` | Verify | DUPLICATE | test-master skill + quality gate (spec-009) cover this |
| `browser-testing-with-devtools` | Verify | SKIP | Requires Chrome DevTools MCP — hardware dependency, skip |
| `performance-optimization` | Verify | SKIP | Core Web Vitals targets — frontend-specific, skip for general preset |
| `observability-and-instrumentation` | Verify | ADAPT | "Define working before instrumenting" checklist; good for ship phase |
| `code-review-and-quality` | Review | DUPLICATE | code-reviewer skill + /code-review command cover this |
| `code-simplification` | Review | DUPLICATE | /simplify command covers this |
| `security-and-hardening` | Review | DUPLICATE | security-review skill covers this |
| `documentation-and-adrs` | Review | ADAPT | ADR template + "document the why" discipline; missing from defaults |
| `git-workflow-and-versioning` | Review | SKIP | Trunk-based + atomic commits — already in executor prompt as rules |
| `ci-cd-and-automation` | Ship | SKIP | GitHub Actions YAML — infra-specific, not executor prompt material |
| `shipping-and-launch` | Ship | ADAPT | Pre-launch checklist (code/security/perf/infra/rollback) |
| `deprecation-and-migration` | Ship | DUPLICATE | Covered by project-audit + board lifecycle; niche enough to skip |

---

## ADAPT Details

### 1. `spec-driven-development`

**What to take:** The assumption-surfacing block (`ASSUMPTIONS I'M MAKING: … → Correct me now`)
plus the six core spec areas (Objective, Commands, Architecture, Data model, API, Tasks).

**Where:** New `data/prompts.json` entry `"spec-writer"` (category Топ or Define).

**Draft prompt:**

```
You are a spec-writer. Before drafting anything, surface your assumptions explicitly:

ASSUMPTIONS I'M MAKING:
1. [tech stack / environment]
2. [auth model / data layer]
3. [scope boundary]
→ Correct me now or I'll proceed with these.

Then write a spec covering: Objective (who/why/success), Commands (exact CLI flags),
Architecture (component boundaries), Data model (key entities), API surface (endpoints/
contracts), Task breakdown (ordered, verifiable). Each section max 10 lines unless depth
is required. Output to specs/spec-NNN-<name>.md.
```

---

### 2. `planning-and-task-breakdown`

**What to take:** The dependency-graph step ("build foundations first") and the instruction
to operate read-only during planning ("do NOT write code during planning").

**Where:** Executor pre-run system prompt addendum; also useful in `"project-planner"`
prompt template.

**Draft prompt addendum:**

```
PLANNING MODE — read-only. Map the dependency graph before writing any code:
  schema → models → endpoints → client → UI
Implement bottom-up. Each task: title + acceptance criteria + test signal. Max 1 day per task.
```

---

### 3. `doubt-driven-development`

**What to take:** The non-trivial decision test (branching logic / module boundary /
correctness the compiler can't verify / irreversible blast radius) and the 5-step
doubt cycle checklist.

**Where:** Executor prompt footer ("before committing non-trivial changes, run the doubt
cycle"); or a separate `"doubt-check"` prompt template for gated runs.

**Draft prompt addendum:**

```
Before committing: is this decision non-trivial?
  - New branching logic? Crosses module boundary? Compiler cannot verify correctness?
  - Irreversible in production?
If YES → doubt cycle:
  [ ] Claim: what does this change assert?
  [ ] Contract: what must remain true?
  [ ] Adversarial: one concrete way this could be wrong
  [ ] Reconcile: is the finding real or already handled?
  [ ] Stop: trivial findings / 3 cycles / explicit override
```

---

### 4. `source-driven-development`

**What to take:** The DETECT → FETCH → IMPLEMENT → CITE loop and the explicit stack
declaration step before writing framework-specific code.

**Where:** Executor system prompt (applies whenever the executor touches a framework);
also as `"source-driven"` prompt template.

**Draft prompt addendum:**

```
Before writing framework-specific code:
  STACK DETECTED: [read package.json / pyproject.toml / go.mod — state exact versions]
  → Fetch official docs for the relevant pattern (WebFetch / WebSearch).
  → Implement only what the docs describe. Cite the URL in a comment.
Training data goes stale. Verify, don't assume.
```

---

### 5. `debugging-and-error-recovery`

**What to take:** The Stop-the-Line rule and the triage checklist order (Reproduce → Isolate →
Root cause → Fix → Guard → Verify).

**Where:** Executor error-handler prompt path; also `"debug-triage"` prompt template.

**Draft prompt:**

```
Something broke. Stop-the-Line protocol:
1. STOP adding features or making other changes.
2. PRESERVE evidence: paste exact error, last working commit, env.
3. REPRODUCE: make the failure happen reliably. If not reproducible → document and monitor.
4. ISOLATE: binary-search the change set. Smallest reproducer.
5. ROOT CAUSE: don't fix symptoms. Find why, not what.
6. FIX: targeted, minimal change. Add a regression test.
7. VERIFY: run tests. Confirm fix in the same environment the bug appeared.
8. RESUME only after step 7 passes.
```

---

### 6. `observability-and-instrumentation`

**What to take:** The "define working before instrumenting" discipline and the
metrics vs logs vs traces selection table.

**Where:** `"ship-instrumentation"` prompt template; executor pre-ship checklist.

**Draft prompt:**

```
Before instrumenting, answer: what questions will on-call ask about this feature?
  1. [question]  → signal type (log / metric / trace)
  2. [question]  → signal type
Rule: metrics tell you THAT something is wrong; traces tell you WHERE; logs tell you WHY.
Emit structured events (stable name + machine-readable fields), not prose strings.
Never log secrets or PII.
```

---

### 7. `documentation-and-adrs`

**What to take:** The ADR template (Status / Date / Context / Decision / Alternatives /
Consequences) and the "document the why, not the what" principle.

**Where:** `"adr-writer"` prompt template; executor post-implementation step when
an architectural decision was made.

**Draft prompt:**

```
Record this architectural decision as an ADR in docs/decisions/ADR-NNN-<title>.md:

## Status: Accepted
## Date: YYYY-MM-DD
## Context: [what problem, what constraints, what forced the decision]
## Decision: [what was chosen and why — not just "we chose X"]
## Alternatives considered: [what else was on the table and why rejected]
## Consequences: [what gets easier, what gets harder, what monitoring is needed]

Keep it under 400 words. The goal: a future engineer can understand the why without
asking the author.
```

---

### 8. `shipping-and-launch`

**What to take:** The pre-launch checklist structure (Code Quality / Security / Performance /
Infrastructure / Rollback). Distill into a compact executor pre-deploy gate.

**Where:** `"pre-deploy-gate"` prompt template; executor final step before triggering deploy.

**Draft prompt:**

```
Pre-deploy gate — confirm each before deploying:
CODE:   [ ] tests pass  [ ] build clean  [ ] lint/types pass  [ ] no debug console.log
SECURITY: [ ] no secrets in code/git  [ ] npm audit no criticals  [ ] input validation
INFRA:  [ ] env vars set  [ ] migration ran  [ ] rollback plan exists
MONITOR: [ ] error rate baseline noted  [ ] rollback trigger defined (error % or latency)

If any box is unchecked → stop and report. Do not deploy.
```

---

## Personal Install Recommendation (interview-me / idea-refine)

### `interview-me` — **YES, install**

This is the only skill in the pack that explicitly prevents the most common product mistake:
building what was asked rather than what was meant. Its one-question-at-a-time + guess-attached
format produces a hypothesis with a confidence number before any plan or code is written.

For the operator's context (SacTech pitch prep, AI Receptionist scope clarification, new
business ideas with multiple stakeholders), this is directly useful before any spec session.
It pairs cleanly with the existing workflow: interview-me → spec-driven-development →
spec in vault → implementation.

Install as a personal skill at `~/.claude/commands/interview-me.md` (or equivalent).
Trigger phrases already defined in the skill: "interview me", "grill me", "are we sure?",
"stress-test my thinking".

**One caveat:** the skill explicitly disables itself in non-interactive contexts (CI,
scheduled runs, `/loop`). That matches the operator's usage pattern — this is a live
product-thinking tool, not an automation.

### `idea-refine` — **YES, install (lower priority)**

Complements `interview-me` when there's already a rough concept but it needs structured
divergent expansion before converging on a spec. The three-phase structure (Understand &
Expand → Evaluate & Converge → Sharpen & Ship) produces a one-pager markdown
(`docs/ideas/<name>.md`) which feeds directly into the spec workflow.

Especially useful for the operator's B2B positioning work (igdigi, SacTech packages) where
the idea is known but the framing is fuzzy. Lower priority than `interview-me` because
the operator already uses `/clear` + structured conversations for this; `idea-refine` adds
the divergent-variation step that is currently missing.

Install as `~/.claude/commands/idea-refine.md`. Trigger: "help me refine this idea" /
"ideate on [concept]".

---

## What NOT to adapt

- `using-agent-skills` — the conductor (spec-017) already routes by phase.
- `incremental-implementation` — worktree board cycle already enforces slice-by-slice.
- `context-engineering` — CLAUDE.md hierarchy + spec-017 conductor prompt cover this.
- `test-driven-development` — test-master + quality gate (spec-009) cover this.
- `code-review-and-quality` — code-reviewer skill + /code-review cover this.
- `code-simplification` — /simplify covers this.
- `security-and-hardening` — /security-review covers this.
- `deprecation-and-migration` — niche; project-audit + board lifecycle sufficient.
- `browser-testing-with-devtools` — hardware dependency (Chrome DevTools MCP).
- `frontend-ui-engineering`, `performance-optimization` — stack-specific, not generic.
- `api-and-interface-design` — too wide; useful reference but not a compact preset.
- `ci-cd-and-automation` — infra-specific YAML; not executor prompt material.
- `git-workflow-and-versioning` — already in executor prompt as commit rules.

---

## Integration Roadmap

Per the task card: these playbooks plug into the sub-agent Settings UI (spec-017 Phase C).
Suggested sequencing:

1. ~~Add `spec-writer`, `debug-triage`, `pre-deploy-gate` to `data/prompts.json` as
   default templates (immediately useful for any OSS user).~~ **DONE 2026-06-11.**
   Implemented as `DEFAULT_PROMPT_TEMPLATES` constant + `_seed_default_prompts()` in
   `webapp.py`. Seed merges on every startup: inserts absent defaults, skips existing ones,
   never re-inserts operator-deleted entries (tracked via `__deleted_defaults` in the file).
   Attribution comment and README Credits section added. 22 tests green.

2. ~~Add executor system-prompt addendums (planning mode, source-driven, doubt-check) to
   the default `AgentDefinition.prompt` for the `executor` role in the spec-017 roster.~~ **DONE 2026-06-11.**
   Three addendums (PLANNING MODE, SOURCE-DRIVEN, DOUBT CHECK) added to `DEFAULT_AGENTS["executor"].prompt`
   in `bot.py`. researcher/quick prompts unchanged. Covered by tests.

3. Ship `interview-me` and `idea-refine` as personal operator installs (not in OSS
   default — they require a live interactive user).
4. Add `adr-writer` and `observability` templates in a follow-up pass.
