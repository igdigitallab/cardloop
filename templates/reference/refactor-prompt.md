# Refactor Prompt — project refactoring plan

Run **after** an audit (audit-<date>.md already exists) and **after** the baseline is covered (error alerting present + tests on critical paths). Without this — STOP.

Prompt for copy-paste into a new Claude Code session. Replace `<PROJECT>` with the project name.

---

## Prompt

```
# Refactor task: <PROJECT>

Project: $HOME/<PROJECT>/
Read:
1. The project's CLAUDE.md
2. $VAULT/01-Projects/<PROJECT>/README.md
3. $VAULT/01-Projects/<PROJECT>/audit-*.md — the most recent audit report (if several — pick the newest)
4. $VAULT/03-Resources/_templates/project-baseline.md
5. The "Tech gotchas" section in $HOME/CLAUDE.md

Apply `legacy-modernizer` skill principles (if installed) — strangler fig, characterization tests, incremental migration.

## Mode: PLAN ONLY on the first pass
Do NOT change code. Create a refactoring plan → spec in `$VAULT/01-Projects/<PROJECT>/specs/`. After that — the operator reviews, approves, and we proceed phase by phase.

---

## Stage 0: Pre-flight gate (BLOCKERS)

Before planning any refactoring — verify the preconditions are met:

1. **Audit report exists and is fresh** (<30 days). If not → STOP, "run audit first: use audit-prompt"
2. **All P0 items from the audit are fixed** (no open critical vulnerabilities). If any remain → STOP, "fix P0s first"
3. **Baseline is covered:**
   - Error→Claude alerting is working (not just present in code — actually sending alerts; verify in logs)
   - Tests on critical paths exist and are green (`pytest` passes)
   - `.env.example` is current
4. **Coverage of critical paths ≥ 80%** (per the list in the project's CLAUDE.md). If below — not a STOP, but the first refactoring phase = "write characterization tests"

If any blocker is not met — report what is blocking and propose what to do first.

---

## Stage 1: Scope definition

What to refactor — determine from the audit report:
- **P1 findings rooted in architecture** → primary refactoring candidates
- **Accumulated P2 findings** → secondary candidates
- **Homegrown solutions where a ready-made alternative exists** (e.g., `requests`+`BeautifulSoup` → Firecrawl)
- **Areas with frequent bug fixes** (check `git log --since="3 months ago" --oneline | grep -iE "fix|bug"`)

Do not do "refactor the whole project" — that is a big bang anti-pattern. Work **zonally**: one subsystem at a time.

List of zones in priority order (top 3–5), for each zone:
- **Name** (e.g., "Form validation pipeline")
- **Files** (concrete paths)
- **What's wrong** (1–2 sentences)
- **Target state** (what it should look like)
- **Size**: S (1–2 days) / M (3–7 days) / L (>7 days)
- **Risk**: low / medium / high (depends on how hot the path is)

---

## Stage 2: Characterization tests (golden master) for each zone

Before changing code — capture the current behavior in tests.

Principle from `legacy-modernizer/SKILL.md`: characterization tests document the **existing** behavior (including bugs), not the ideal. Their purpose is to catch regressions, not validate correctness.

For each zone:
- Minimum 5–10 tests covering the happy path + edge cases
- Tests must be **green on the current code** (this is the baseline)
- Do not mock internal components of the zone — only mock external boundaries (Telegram API, DB, filesystem)
- Capture output snapshots (golden master) if a function returns complex structures

If a zone is large (L) — characterization may be a separate phase on its own. That is expected.

---

## Stage 3: Strangler Fig plan

For each zone — incremental migration via facade + feature flag:

```python
# Pseudo
USE_NEW_X = os.getenv("USE_NEW_X", "false").lower() == "true"

def do_x(args):
    if USE_NEW_X:
        return new_implementation.do_x(args)
    return legacy_implementation.do_x(args)
```

Phases for each zone:
1. **Build new in parallel** — new implementation alongside legacy, under feature flag (default off)
2. **Shadow mode** — feature flag enables new, but the result is compared to legacy and divergence is logged (not used in production)
3. **Gradual rollout** — 10% → 25% → 50% → 100% by traffic/user share
4. **Cleanup** — after one week at 100% with no alerts — delete legacy + feature flag

For each phase:
- **Rollback trigger** (what will cause a rollback): new errors in logs, latency growth, user complaints
- **Validation** (what to verify before moving to the next phase): error rate < baseline, key metrics stable
- **Owner** — who is monitoring (operator and/or monitoring system)

If a zone is small (S) and risk is low — the Strangler can be simplified to: "new branch → tests green → merge → deploy → 24h monitoring → cleanup".

---

## Stage 4: What NOT to do (explicit anti-patterns)

- ❌ **Big bang rewrite** — rewrite the entire module and swap it in at once
- ❌ **Refactor around a bug** — found a bug, "while we're at it" tidied up 10 neighboring files
- ❌ **Changing API/contracts under the guise of refactoring** — if behavior changes, that's a redesign, not a refactor
- ❌ **Refactoring a hot path without a feature flag** — even green tests don't protect against production load
- ❌ **Deleting legacy before 100% rollout has been stable for a week** — rollback must be possible
- ❌ **Silently bumping dependencies** — version bumps = a separate PR with tests

---

## Stage 5: Spec file

Create `$VAULT/01-Projects/<PROJECT>/specs/<NNNN>-refactor-<YYYY-MM-DD>.md`:

\```markdown
# Refactor Plan — <PROJECT> — <YYYY-MM-DD>

## Pre-flight
- [x] Audit: <audit-file>
- [x] Baseline OK
- [x] Coverage critical paths: <%>

## Scope — Top N zones
### Zone 1: <Name>
- **Files:** path/to/file.py, path/to/other.py
- **What's wrong:** ...
- **Target:** ...
- **Size:** S/M/L
- **Risk:** low/medium/high

### Zone 2: ...

## Phase plan

### Phase 1: Characterization tests
- [ ] Tests for Zone 1 (target: 10 tests)
- [ ] Tests for Zone 2 (target: 8 tests)
- **Exit criteria:** all green on current code

### Phase 2: Build new in parallel — Zone 1
- [ ] Feature flag `USE_NEW_<ZONE1>` created
- [ ] New implementation `<file>` written
- [ ] All characterization tests green with feature flag=true
- **Exit criteria:** divergence in shadow mode < 0.1%

### Phase 3: Gradual rollout — Zone 1
- [ ] 10% → 24h monitoring
- [ ] 25% → 24h
- [ ] 50% → 48h
- [ ] 100% → 7 days
- **Rollback triggers:** error rate >2x baseline, new error types in logs, user complaints
- **Exit criteria:** 7 days at 100% with no incidents

### Phase 4: Cleanup — Zone 1
- [ ] Delete legacy
- [ ] Delete feature flag
- [ ] Update README + project CLAUDE.md

### Phase 5+: Zone 2, Zone 3, ...

## Estimated timeline
- Phase 1: X days
- Phase 2: Y days
- ...
- Total: Z weeks

## What is NOT in scope for this refactor
(Explicitly list — what goes to backlog, another spec, or will not be done at all)
\```

After creating the spec — briefly report:
"Refactor plan ready: $VAULT/01-Projects/<PROJECT>/specs/<NNNN>-refactor-<date>.md. N zones. Total: Z weeks. Start — Phase 1 (characterization tests)."

---

## After the plan

The operator reads the spec; on approval — a separate session for each phase:
- "Do Phase 1 from spec <NNNN>" — Claude writes characterization tests
- "Do Phase 2 zone 1" — Claude builds the parallel implementation
- and so on

Each phase is a separate session with a clean context + a link to the spec.
```

---

## Related templates

- [[audit-prompt]] — mandatory prerequisite
- [[project-baseline]] — must be green
- [[triage-prompt]] — ranking all projects
