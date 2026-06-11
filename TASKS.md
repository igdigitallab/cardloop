# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

> 2026-05-31: рефакторинг-проход закрыт — 25 карточек (security, рефактор бэк/фронт, OSS-доки, тесты 207→300) ушли в DONE.md. Карта кода → ARCHITECTURE.md. Ниже — только фичи/roadmap.

## Backlog
- [ ] Решить вопрос с удалением проекта. В том числе и, допустим, надо при удалении решать полностью удалять проекты, все смен <!--ops:207822-->
  > ные файлы с ним. Вижу, просто удаляем проект из. Claude ops. Нужен твой совет.
- [ ] Глобальные ключи + общий UI хранилища credentials <!--ops:a7b2c1-->
  > Проектные секреты (.claude-ops/secrets/, UI вкладка, доступ агенту) — СДЕЛАНО в Spec 007. Осталось: глобальные ключи (кросс-проектные) + единый UI.
- [ ] Thinking mode: селектор режима мышления (max/min/default) в чате <!--ops:4df23a-->
  > Версии моделей (Opus 4.8/Sonnet 4.6/Haiku 4.5) + убран дубль селектора — СДЕЛАНО в spec-011 Ф2. Осталось: режимы мышления.
- [ ] multi-chat: несколько чатов на один проект, каждый со своим session_id; полоса вкладок чатов <!--ops:3a00f3-->
- [ ] Карточки: своя свежая сессия на карточку + cwd-замок (развести с общей сессией чата перед автономией) <!--ops:2a0a1a-->

## In Progress

## Review
- [?] TG-канал: очередь TG-сообщений (bot.py on_message — второе сообщение в очередь, не «уже работаю») <!--ops:b53401-->
  > DONE: FIFO per-topic queue in bot.py (_TG_QUEUE / data/tg_queue.json). on_message → enqueue + ack «⏳ Queued #N» when busy; safe_run.finally → _drain_tg_queue → _safe_run_queued (chain drain); /reset clears queue; TG_QUEUE_MAX=5 (env). 16 unit tests in tests/test_tg_queue.py. Patterns reused from webapp.py card queue (sync mutation + atomic flush). Commit: see feat(tg) message-queue.
- [?] TG-канал: проверить, что контекст наполняется корректно и НЕ дублируется при каждом сообщении (была проблема в веб-версии — кэш дублировался и сбрасывался, контекст «съедался»); сравнить с веб-каналом и зафиксировать тестом <!--ops:9aa43f-->
  > VERDICT: NO BUG. (a) resume_session_id: saved in sessions.json after each turn, passed correctly on turn N+1, NOT cleared on error event — only /reset clears it (bot.py:786-788). (b) system_prompt: fresh dict literal each call in run_engine (line 544) and run_agent (line 752) — cannot accumulate; TELEGRAM_NUDGE is an immutable string. TG and web paths produce identical system_prompt. (c) context_tokens: live check on autotopic-test shows 1.01x growth per turn (36497→36689 tokens), not 2x. Session resume confirmed: turn 2 correctly recalled turn 1 content from resuming the same session. Web bug was a FRONTEND busActiveRef reset (ChatTab unmount vs display:none) — unrelated to backend session plumbing. 9 unit tests added in tests/test_tg_session_resume.py covering all three hypotheses.
- [ ] Вкладку обзор удалить вообще. Всю информацию вынести в настройки. Настройки проекта. Ну и во-первых, путь. И все остальное уже есть у нас. И посмотри, вот там есть кнопки аудит проекта и подогнать под проект. Почему так? Не на всех проектах отображаются одинаковые эти кнопки. Это сделано у нас по шаблону и во всех проектах должно быть одинаково. <!--ops:d124ae-->
- [ ] Сделать в чате выделение моих сообщений. Прям всю полосу Headlight какой нибудь. <!--ops:c14fec-->
- [ ] Добавить уведомления - что ответ от AI готов во вкладки наверху. <!--ops:09d84b-->

## Failed
