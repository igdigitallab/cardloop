# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Сейчас у нас только наверху в карточке проекта Но в самом проекте Я имею в виду отображается гид коммит сделан или не сделал Но возможно как-то стоит улучшить вообще интеграцию скитхабом то есть может быть там отображать где-то ещё историю версии или что-нибудь э даже файл ну я не знаю вот как лучше всего сделать так чтобы было максимально удобно пользоваться моим сервисом удобно и чтобы он был самый главный суперфункциональным то есть мы делаем сервис для разработки и общения для создания ну не только для разработки но и всё равно будет подключаться github даже для простоведения переписки там ведение проектов не обязательно чтобы это было разработка <!--ops:ce9a15-->
- [ ] Ещё беклог нет привязки по ширине смартфона телефон через мобильную версию в общем отображается плохо все карточки уезжают вправо <!--ops:7f6ff6-->
- [ ] Нам в чат можно подгрузить картинку причём там подгружается путь до картинки я хочу сделать так же чтобы можно было подгружать картинки в бэк Лок для задач ну там не только картинки файл картинки и прочее то есть <!--ops:74aaac-->
- [ ] Иногда агенты умеют вешать сервисные мониторы на какие-то задачи и в через терминал через Клод клиента эти сервисные мониторы отображаются их можно видеть я хочу сделать так чтобы можно было увидеть эти сервисные мониторы и у нас <!--ops:b6f5cc-->
- [ ] Допустим я сделал OpenSource этот проект. Как его поднимать или я хочу сделать своей племяннице отдельную VM с ее ClaudeOps, там будет полностью VM ее задачи, проекты и прочее и ее подписка на Claude. Как это сделать? <!--ops:2dca4d-->
- [ ] Сделать возможность управления Antigravity CLI и Codex CLI <!--ops:04f84f-->
  > Пропущено по запросу Игоря (2026-06-13) — единственная оставшаяся в Backlog.
- [ ] Spec-040 Phase C: cockpit push заменяет TG-уведомления <!--ops:0ec79e-->
  > Replace TG notifications with cockpit push. Phases C→D→E→F, modify _notify_operator calls. Must precede PTB removal. Separate session required.
  > ЧАСТИЧНО СДЕЛАНО (2026-06-19, spec-041 B5, commit be22b3a): _notify_operator теперь публикует kind:"notification" на глобальную шину → App.tsx показывает тост (operator-push для deferred lifecycle закрыт). ОСТАЛОСЬ в Phase C: прочие TG-push пути (_send_tg_ping/_notify_tg) + при желании Web Push. Порядок 0→B→C→D→E→F жёсткий, D до C нельзя.
- [ ] Fix: Russian literals in webapp.py logic (English-only spec-014) <!--ops:45ae3c-->
  > Point 2: if val not in {"нет","no",...} at line 8849 violates English-only. Related: detector strings in 8905/8920 (пограничный)
- [ ] Fix: Hardcoded Coolify UUID default in schedules.py (spec-014) <!--ops:58412e-->
  > Point 3: schedules.py:718 COOLIFY_SERVER_UUID default="f0kgss8ccgksokkscgc0sk4s" is live infra UUID. Violates anti-hardcode; move to .env/config
- [ ] Design: surface external background processes (Agy/Codex/long bash) in the cockpit <!--ops:6c9a57-->
  > Когда агент запускает внешний фоновый процесс (`ask`/`agy`, Codex, долгий bash run_in_background) — кокпит НЕ показывает, что что-то выполняется: нет индикатора/строки «running», нельзя увидеть прогресс или остановить. Сейчас видно только обычные ходы агента. Придумать решение: реестр активных внешних процессов (pid/команда/старт/статус) + индикатор в UI (баннер/спиннер/панель), желательно с возможностью stop и хвостом вывода. Учесть, что процессы детачнутые, могут пережить ход. Связано с картой 04f84f (управление Antigravity/Codex CLI).
- [ ] Bug: Sub-agent activity not visible in cockpit <!--ops:5ba5ac-->
  > Orchestrator spawns agents but indicator doesn't show activity; user can't tell work is happening
- [ ] Bug: Ghost 'running' indicator not resetting <!--ops:3cfeb7-->
  > Status stuck on 'running' when completion signal is lost (related to 784e8e)
- [ ] Feature: Conversable orchestrator while sub-agents work <!--ops:035154-->
  > Allow user to continue chat with main orchestrator instead of blocking until all agents finish. Currently queued messages wait for completion.
- [ ] Feature: Make global settings easily accessible <!--ops:921e4a-->
  > Surface global settings UI (currently hidden in project context menu) with dedicated entry point. Should include default thinking depth setting.
- [ ] Fix: Global CLAUDE.md blocks sub-agent code execution <!--ops:30db09-->
  > ~/CLAUDE.md 'no code, delegate to agents' rule applies globally to all agents; needs role-based gating so executors can code without delegating
- [ ] P0 Sanitize OPSEC/khronika names in HEAD <!--ops:b2e1d1-->
  > spec-041 §0.1 RED ALERT. khronika in tests/test_forum_topic.py:193,198 (live fixture), specs/spec-011:38, spec-018:31, spec-030:69; line_vpn_bot in templates/reference/project-baseline.md:16-48. Replace with neutral names.
- [ ] P0 Sanitize personal paths/domains/names in HEAD <!--ops:c3d2e2-->
  > spec-041 §0.5 full list: test_janitor_quarantine.py:17, test_is_destructive.py:37, test_phase0_session_keys.py, test_schedules.py:875,906-968 (proxmon-bot/pyrogram_bot), board.py:10 (github.com/igor URL), GOTCHAS.md:53, CLAUDE.md:7, TASKS.md (incl. card ops:2dca4d 'племяннице'), DONE.md:94, research-agent-skills.md.
- [ ] P0 Untrack claude-ops-bot.service, document .service.template <!--ops:d4c3f3-->
  > spec-041 §0.4. git rm --cached the .service (User=igor, /home/igor/...), gitignore it, point README/CONTRIBUTING at the existing __USER__ template.
- [ ] P0 Decide fate of internal docs (DONE/TASKS/CLAUDE/GOTCHAS/specs) <!--ops:e5b4a4-->
  > spec-041 §0.6. Keep ARCHITECTURE/README/CONTRIBUTING/docs/API (English); sanitize GOTCHAS; move TASKS.md+DONE.md to docs/internal/ (gitignored or private branch); keep specs/ only after sanitizing.
  > NOTE: §0.3 hardcoded Coolify UUID is already tracked as card ops:58412e — do not duplicate.
- [ ] P0 ToS compliance notice + make API-key auth first-class <!--ops:071c6c-->
  > spec-041 §0.8 (real legal risk). Hosting for OTHER users on their subscriptions is banned (OpenClaw). README ToS Notice: official CLI only, user owns compliance, multi-user/commercial → ANTHROPIC_API_KEY. Document bot.py:65 env-pop; make API-key mode a documented option, not a silent pop.
- [ ] P0 SECURITY R1: log_cmd/test_cmd = arbitrary RCE <!--ops:4b50a0-->
  > spec-041 §0-C, CVSS 9.8. webapp.py:3401,2506 — value stored raw, shlex.split + exec by background scanner. Allowlist formats (journalctl -u / docker logs / tail -f <file>); reject shell metachars.
- [ ] P0 SECURITY: endpoint/fs hardening (R6/R7/Y2/Y5/Y1) <!--ops:6d72c2-->
  > spec-041 §0-C. R6 trash restore path not allowlist-checked (webapp.py:1748). R7 no security headers (add middleware: X-Frame-Options DENY, nosniff, Referrer-Policy). Y2 global file API reads ~/.ssh + ~/.claude/.credentials.json (webapp.py:6130) → exclude list. Y5 handoff agents bypassPermissions → default. Y1 recovery codes 32→64 bit (totp.py:161).
- [ ] P1 README hook line + "why this exists" story <!--ops:7e83d3-->
  > spec-041 §1.1. Killer first line ("the board is your agent's working memory — cards move themselves") + personal narrative + badges. HN rewards story over feature list.
- [ ] P1 Architecture diagram (mermaid) in README <!--ops:90a5f5-->
  > spec-041 §1.3. Component + data-flow (already drafted in audit). Rare in this niche, signals maturity.
- [ ] P1 Security Model section in README <!--ops:a1b606-->
  > spec-041 §1.4. State plainly: bypassPermissions=full host access by design, single-user, sub-vs-API auth, R1/R2/Y2 facts, HTTPS+WEB_COOKIE_SECURE required. Candor = maturity.
- [ ] P1 README positioning content (differentiation + sub-as-feature + how-the-board-works) <!--ops:b2c717-->
  > spec-041 §1.5/1.6/1.7. 4-5 line "how this is different" (board=source of truth, agents update it); reframe subscription-auth as a FEATURE not a warning; "How the board works" with a real TASKS.md example.
- [ ] P2 Create requirements.txt (prod deps, pinned) <!--ops:d4e939-->
  > spec-041 §2.1. Only requirements-dev.txt exists. Pin from live venv: claude-agent-sdk>=0.2.96, python-telegram-bot==22.7, aiohttp==3.13.5, APScheduler==3.11.2, cryptography>=48, python-dotenv, anyio. State Python ≥3.11.
- [ ] P2 Verify quickstart on a CLEAN machine + .env.example (web too) <!--ops:e5fa4a-->
  > spec-041 §2.2. Walk install as a stranger (not author box). Add web/.env.example documenting VITE_BACKEND_URL; human comment per var.
- [ ] P2 .github/ CI with venv bootstrap + issue/PR templates <!--ops:0a1c6d-->
  > spec-041 §2.4. CI must: python -m venv venv && venv/bin/pip install -r requirements-dev.txt && env -u WEB_COOKIE_SECURE venv/bin/python -m pytest. GOTCHA: needs venv python or pytest-aiohttp missing → ~237 tests error. Add make setup.
- [ ] P2 Dependency-scan hygiene: vite bump + pip-audit + pytest filterwarnings <!--ops:182d8e-->
  > spec-041 §2.5. npm audit flags vite<=6.4.2 HIGH (dev-server only, but scary at install) → vite@8. Add pip-audit CI step. pytest.ini: filterwarnings ignore RuntimeWarning:unittest.mock (10 noisy).
- [ ] P2 Fix CI-breaking test_janitor_quarantine <!--ops:293e9f-->
  > spec-041 §2.6. tests/test_janitor_quarantine.py:17 references external /home/igor/server-janitor/... → FileNotFoundError on any CI. Skip-with-reason, vendor script, or mock.
- [ ] P3 Conditional board injection (root cause of "agents stumble") <!--ops:3a4fa0-->
  > spec-041 §3.1 (pull-forward candidate to P2). engine.py:1164-1171 injects board_summary (~3K tokens) + BOARD_PROTOCOL + 3 CLAUDE.md EVERY turn. Inject only when relevant (backlog>0/param). Card-runs already ephemeral; plain chat not.
- [ ] P3 Remove dead code (auto-rotation/auto-resume/stall corpses) <!--ops:4b50b1-->
  > spec-041 §3.2. Dead constants CONTEXT_ROTATE/WARN/ROTATION dup'd engine.py:70 + webapp.py:67. Dead _AUTO_RESUME_*, _card_last_result_event, _tg_last_result_event, _maybe_auto_resume, STALL_SECONDS, stalled{}.
- [ ] P3 Fix blocking subprocess.run in async cmd_diff <!--ops:5c61c2-->
  > spec-041 §3.3. bot.py:~1017 sync subprocess.run(timeout=15) blocks event loop → asyncio.create_subprocess_exec + await communicate().
- [ ] P3 De-duplicate definitions <!--ops:6d72d3-->
  > spec-041 §3.4. _README_CANDIDATES (2× webapp), _ALLOWED_CARD_MODELS (board.py:55↔webapp), _OPS_SCRATCH_CWD (engine+webapp:58), _RUNNABLE literal, buried import hmac (webapp:447), inlined session_key (~15×)→helper.
- [ ] P3 Frontend declutter: split ChatTab.tsx + PWA polish <!--ops:7e83e4-->
  > spec-041 §3.5. Split ChatTab.tsx (2639 lines) → SessionBar/MessageFeed/ChatComposer/DeferredRunsModal. Remove dead overview.css; extract lib/storage.ts; pin lucide-react off pre-2.0. 44px touch targets (chat.css:1244), <html lang> fix, maskable icon + id in manifest.json.
- [ ] P3 Decompose webapp.py into backend/ modules (gated on baseline) <!--ops:8f94f5-->
  > spec-041 §3.6. 10,144 lines → projects/board_api/chat/secrets_api/files/timeline/schedules_api/core. ctx-dict contract stays (not breaking). Plus: extract run_engine session runner; type AppCtx as dataclass; storage.py write_atomic for save_topics/sessions. Needs baseline (triage→audit→refactor).
- [ ] P3 Test coverage gaps (secret.py 0%, bot.py 28%) <!--ops:90a606-->
  > spec-041 §3.7. secret.py key-storage CLI has NO tests — add tests/test_secret_cli.py (subprocess set/get/list/delete), high priority (trust anchor). bot.py 28% (TG transport), schedules.py 59% (systemd). 2 slow tests (13.3s+8s) in rotate/handoff.
- [ ] P3 Document/fix architecture debt (state-on-restart, single-user, schema-version) <!--ops:a1b717-->
  > spec-041 §3.8. running{}/_live_clients{} lost on restart → cards stuck In Progress (no indicator). Single-user hardcoded key_of(cwd)=basename (engine.py:303) → collision data-loss. No data-schema versioning. Circular imports unchecked. Surface first three in Security Model.

## In Progress
- [~] сделать нормальный логин и пароль. ? <!--ops:e518e2-->
- [~] Redesign project cards UX: kebab menu + drag handle <!--ops:b88c94-->
  > Rethink card interaction: explicit ⋯ menu (not long-press), grip handle (⠿) for drag-only, mobile bottom-sheet actions. Scope choice: both sidebar+tabs vs sidebar-only first.
- [~] P0 Rewrite git history: --mailmap (author email) + --replace-text <!--ops:a1f0c0-->
  > spec-041 §0.2. Author field of EVERY commit is `Igor <zira777ru@gmail.com>` → needs git filter-repo --mailmap (replace-text does NOT touch author headers). Plus --replace-text list: 282311426, @ziraclaudebot, ops-igor-2026 (old pwd), tg.session path, 1780365319, pve, proxmox-tunnel, coscore.us/crm/firecrawl, /home/igor. Orphan/squash NOT needed. Coordinate force-push; verify with git grep over --all after.

## Review
- [?] Feature: photo upload to chat with Ode viewer <!--ops:4b020c-->
  > Support sending photos directly from cockpit to chat and opening in Ode app
- [?] Fix: mobile screenshot viewer close interaction <!--ops:5fa4f2-->
  > Tap anywhere/system back closes viewer without exiting service; 48×48 close button with safe area consideration
- [?] Fix: api_project_rotate must clear session_id in chats.json <!--ops:dff9aa-->
  > Root cause confirmed: api_project_rotate clears sessions.json (layer 2) but not chats.json; active named chat retains old session_id which gets mirrored back by _mirror_active_chat_to_sessions, preventing reset. Solution: zero out session_id of active chat under _chats_lock().
- [?] Fix: mobile network error after screen wake (EventSource + error banner) <!--ops:9b71e9-->
  > Root cause: (1) EventSource at App.tsx:316 not recreated on wake—App.tsx:367 onerror handler doesn't trigger reconnect after iOS sleep; (2) Stuck error banner at ChatTab.tsx:1236 persists until page reload. Solution: Recreate EventSource on visibilitychange/focus, clear error state, add /api/me health check on wake. Related: spec-035 (reconnect-cursor).
- [?] Fix: mobile dropdown backdrop and positioning <!--ops:e1db52-->
  > commit 81ce53e: Added dimmed backdrop for mobile bottom-sheet, aligned breakpoints at ≤768px
- [?] UI: Show actual thinking level instead of 'default' <!--ops:90a27f-->
  > Display resolved effort level (e.g. 'medium') in header instead of 'default' so user knows current setting. Currently DEFAULT_EFFORT=medium is invisible.
- [?] Implement spec-045: Chat window densification <!--ops:8f65ea-->
  > Move collapse button ⟩ to end of toolbar, reduce chat window from 5 to 2 rows. Spec: ~/vault/01-Projects/Cardloop-Bot/specs/spec-045-chat-window-densification.md
- [?] /compact visual feedback during compression <!--ops:96ccc7-->
  > Show progress indicator when /compact agent is running compression (30-60s) to prevent UX confusion from appearing frozen
- [?] Global redesign: Graphite & Chalk <!--ops:79b153-->
  > CSS and font infrastructure
- [?] Fix: increase mobile font size (14→15px on ≤768px) <!--ops:94d30c-->
  > Bumped body 14→15px and chat messages 13→15px on mobile breakpoint for better readability on phones
- [?] Update: 3 principles consolidated in 'Scan, don't assume' <!--ops:1aef51-->
  > Injected Carpathian contract principles (read-don't-guess, surgical diffs, simpler-better) into CLAUDE.md line
- [?] spec-046 Phase A: Project templates + git init automation <!--ops:52468e-->
  > Create English-only lean templates (5 archetypes), .gitignore stubs, log_cmd/test_cmd stubs, git init on project creation. Spec: ~/vault/01-Projects/Cardloop-Bot/specs/spec-046-project-creation-automation.md — answer 4 design questions before starting
- [?] Implement spec-046: project creation redesign (full) <!--ops:463ddc-->
  > Full implementation completed: templates, backend creation, onboarding, UUID stability, free-chat unification, frontend intent field, i18n, tests with backward compatibility
- [?] Spec-046 Phase A: Frontend intent field + welcome screen <!--ops:0cdf6a-->
  > Connected frontend intent/type to backend API; added 'What do you want to work on?' field on welcome screen; CSS in Graphite & Chalk; committed 46218e4
- [?] P1 Record 20-second demo GIF (highest-leverage asset) <!--ops:8f94e4-->
  > spec-041 §1.2. Wow-moment: card→In Progress→agent works→diff appears→ping. First 15s = the hook. Embed top of README.
- [?] Build and verify Docker image (Dockerfile check) <!--ops:5dae23-->
- [?] Fix: npm ci fails on eslint peer dependency conflict <!--ops:69713f-->
  > Aligned @eslint/js@^9 with eslint@^9 in web/package.json, regenerated lock for docker build compatibility. Commit 2fde1f3. Rebuild in progress.

## Failed
- [!] [ERR] ERROR: asyncio Task exception was never retrieved <!--ops:err-00e3e8-->
  > source=log
  > seen=2
  > first=2026-06-18T11:33
  > last=2026-06-18T13:33
  > excerpt=asyncio Task exception was never retrieved
- [!] [ERR] ERROR: claude_agent_sdk._internal.query Fatal error in message reader: Command failed w <!--ops:err-538a69-->
  > source=log
  > seen=3
  > first=2026-06-19T19:50
  > last=2026-06-21T18:32
  > excerpt=claude_agent_sdk._internal.query Fatal error in message reader: Command failed with exit code 143 (exit code: 143)
- [!] сделать возможность переноса вкладок проектов (которые наверху), т.е. менять местами. <!--ops:ab5479-->
