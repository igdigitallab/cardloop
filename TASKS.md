# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Сделать возможность управления Antigravity CLI и Codex CLI <!--ops:04f84f-->
  > Пропущено по запросу Игоря (2026-06-13) — единственная оставшаяся в Backlog.

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
