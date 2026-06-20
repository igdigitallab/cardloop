# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Глобальный редизайн: https://styles.refero.design/style/0fd67ec5-7e9c-4ca9-b368-5d9c7388477a <!--ops:56979f-->
- [ ] сделать возможность переноса вкладок проектов (которые наверху), т.е. менять местами. <!--ops:ab5479-->
- [ ] сделать нормальный логин и пароль. ? <!--ops:e518e2-->
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

## In Progress
- [~] /compact visual feedback during compression <!--ops:96ccc7-->
  > Show progress indicator when /compact agent is running compression (30-60s) to prevent UX confusion from appearing frozen

## Review
- [?] Feature: photo upload to chat with Ode viewer <!--ops:4b020c-->
  > Support sending photos directly from cockpit to chat and opening in Ode app
- [?] Fix: mobile screenshot viewer close interaction <!--ops:5fa4f2-->
  > Tap anywhere/system back closes viewer without exiting service; 48×48 close button with safe area consideration
- [?] Fix: mobile sidebar scroll issue (project selection) <!--ops:56a219-->
  > Deployed to :8787. Allow scrolling project list instead of drag interaction. Needs user test confirmation + git commit.
- [?] Fix: api_project_rotate must clear session_id in chats.json <!--ops:dff9aa-->
  > Root cause confirmed: api_project_rotate clears sessions.json (layer 2) but not chats.json; active named chat retains old session_id which gets mirrored back by _mirror_active_chat_to_sessions, preventing reset. Solution: zero out session_id of active chat under _chats_lock().
- [?] Fix: mobile network error after screen wake (EventSource + error banner) <!--ops:9b71e9-->
  > Root cause: (1) EventSource at App.tsx:316 not recreated on wake—App.tsx:367 onerror handler doesn't trigger reconnect after iOS sleep; (2) Stuck error banner at ChatTab.tsx:1236 persists until page reload. Solution: Recreate EventSource on visibilitychange/focus, clear error state, add /api/me health check on wake. Related: spec-035 (reconnect-cursor).
- [?] Fix: mobile dropdown backdrop and positioning <!--ops:e1db52-->
  > commit 81ce53e: Added dimmed backdrop for mobile bottom-sheet, aligned breakpoints at ≤768px

## Failed
- [!] [ERR] ERROR: asyncio Task exception was never retrieved <!--ops:err-00e3e8-->
  > source=log
  > seen=2
  > first=2026-06-18T11:33
  > last=2026-06-18T13:33
  > excerpt=asyncio Task exception was never retrieved
