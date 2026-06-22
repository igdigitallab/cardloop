# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Я все время забываю, какой задачей мы занимаемся в проекте. Вот, то есть в последней. Я хочу, может быть, как-то куда-то, может быть, выводить, над какой задачей мы работаем. Потому что я вот и каким образом это будет автоматически подтягиваться. И чтобы мне потом вспомнить, мне приходится листать, какую задачу я ставил там, и искать и понимать, вспоминать. <!--ops:e63055-->
- [ ] 99% работу у меня сейчас идет через наш сервис, вот этот, но иногда нужно сделать какие-то выполнить какие-то команды без участия чата или же клода. То есть сделать это просто через терминал. я хочу добавить новый функционал сделать некую может быть добавить просто чистый терминал. Как новая вкладка, чтобы открывался просто чистый терминал. <!--ops:cadb81-->
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
  > 5 Cyrillic detector strings remain in webapp.py (verified 2026-06-22): 9817 {"нет",...} conformance value; 9873 "Правила работы в кокпите"→should match template "Cockpit Rules"; 9888 "Формат карточки"→"Card format"; 9994 docstring. NOT a blind swap — detector heading "## ClaudeOps conformance" (9812) also mismatches template "## ClaudeOps Integration Status". Align detectors to English templates; decide backward-compat for operator's legacy Russian project files (his project-audit would regress). Needs Igor's call on compat.
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
- [ ] P0 Sanitize personal paths/domains/names in HEAD <!--ops:c3d2e2-->
  > spec-041 §0.5. CODE clean (verified 2026-06-22): tests, board.py URL→cardloop/cardloop, no proxmon/pyrogram. RESIDUAL only in internal docs → GOTCHAS.md:53 ('igor в группе adm', Russian), TASKS.md card ops:2dca4d ('племяннице'), DONE.md, specs/spec-041 audit. These belong to card e5b4a4 (internal-docs fate). Close together with e5b4a4.
- [ ] P0 Decide fate of internal docs (DONE/TASKS/CLAUDE/GOTCHAS/specs) <!--ops:e5b4a4-->
  > spec-041 §0.6. Keep ARCHITECTURE/README/CONTRIBUTING/docs/API (English); sanitize GOTCHAS; move TASKS.md+DONE.md to docs/internal/ (gitignored or private branch); keep specs/ only after sanitizing.
  > NOTE: §0.3 hardcoded Coolify UUID is already tracked as card ops:58412e — do not duplicate.
- [ ] P1 Architecture diagram (mermaid) in README <!--ops:90a5f5-->
  > spec-041 §1.3. Component + data-flow (already drafted in audit). Rare in this niche, signals maturity.
- [ ] P2 Verify quickstart on a CLEAN machine + .env.example (web too) <!--ops:e5fa4a-->
  > spec-041 §2.2. Walk install as a stranger (not author box). Add web/.env.example documenting VITE_BACKEND_URL; human comment per var.
- [ ] P2 Dependency-scan hygiene: vite bump (only remaining part) <!--ops:182d8e-->
  > spec-041 §2.5. DONE: pip-audit CI step (ci.yml:37-38), pytest filterwarnings (pytest.ini). REMAINING: vite still ^5.4.10 → bump to vite@8 (3 majors, plugin-API breaking; needs `npm run build` verification). HIGH advisory is dev-server only.
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
