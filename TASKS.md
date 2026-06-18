# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Сделать возможность управления Antigravity CLI и Codex CLI <!--ops:04f84f-->
  > Пропущено по запросу Игоря (2026-06-13) — единственная оставшаяся в Backlog.
- [ ] Spec-040 Phase C: cockpit push заменяет TG-уведомления <!--ops:0ec79e-->
  > Replace TG notifications with cockpit push. Phases C→D→E→F, modify _notify_operator calls. Must precede PTB removal. Separate session required.
- [ ] Fix: Russian literals in webapp.py logic (English-only spec-014) <!--ops:45ae3c-->
  > Point 2: if val not in {"нет","no",...} at line 8849 violates English-only. Related: detector strings in 8905/8920 (пограничный)
- [ ] Fix: Hardcoded Coolify UUID default in schedules.py (spec-014) <!--ops:58412e-->
  > Point 3: schedules.py:718 COOLIFY_SERVER_UUID default="f0kgss8ccgksokkscgc0sk4s" is live infra UUID. Violates anti-hardcode; move to .env/config
- [ ] Design: surface external background processes (Agy/Codex/long bash) in the cockpit <!--ops:6c9a57-->
  > Когда агент запускает внешний фоновый процесс (`ask`/`agy`, Codex, долгий bash run_in_background) — кокпит НЕ показывает, что что-то выполняется: нет индикатора/строки «running», нельзя увидеть прогресс или остановить. Сейчас видно только обычные ходы агента. Придумать решение: реестр активных внешних процессов (pid/команда/старт/статус) + индикатор в UI (баннер/спиннер/панель), желательно с возможностью stop и хвостом вывода. Учесть, что процессы детачнутые, могут пережить ход. Связано с картой 04f84f (управление Antigravity/Codex CLI).

## In Progress

## Review
- [?] UX audit: identify cockpit rough edges <!--ops:60100f-->
  > Live UX walkthrough via Playwright — comprehensive audit of board, chat, settings, logs, files, memory, activity, vault views. Report with prioritized findings generated (report saved).
- [?] Feature: photo upload to chat with Ode viewer <!--ops:4b020c-->
  > Support sending photos directly from cockpit to chat and opening in Ode app
- [?] Fix: mobile screenshot viewer close interaction <!--ops:5fa4f2-->
  > Tap anywhere/system back closes viewer without exiting service; 48×48 close button with safe area consideration
- [?] Fix: mobile sidebar scroll issue (project selection) <!--ops:56a219-->
  > Deployed to :8787. Allow scrolling project list instead of drag interaction. Needs user test confirmation + git commit.

## Failed
- [!] [ERR] ERROR: asyncio Task exception was never retrieved <!--ops:err-00e3e8-->
  > source=log
  > seen=2
  > first=2026-06-18T11:33
  > last=2026-06-18T13:33
  > excerpt=asyncio Task exception was never retrieved
