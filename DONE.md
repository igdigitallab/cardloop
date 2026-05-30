# Done — claude-ops-bot

Архив завершённых карточек (append-only). **Сессии его НЕ читают** — гигиена контекста.

## 2026-05-29
- [x] Live-прогресс карточек в чат-панели через шину событий (activity-stream): прогон карточки виден вживую как «🗂 карточка» <!--ops:livep1--> — E2E на проде
- [x] C1: чат по проекту в кокпите (SSE-стрим, сессия общая с TG) <!--ops:d616af--> — E2E на проде (sandbox резюмит session topic 369)
- [x] F1: авто-запуск карточки — перенос Backlog→In Progress запускает run_engine, авто→Review/Failed + сайдкар + TG-пинг <!--ops:8c3888--> — E2E на проде (sandbox)
- [x] F1: кросс-пинг в TG-топик при завершении карточки (внутри F1) <!--ops:650fb7-->
- [x] F0: рефактор run_agent → async-генератор `run_engine` (TG/glasses/web — один движок) <!--ops:f65e6e--> — E2E на проде, sandbox+Networking-OS
- [x] M1/B1+B2: baseline-каркас + уровни зрелости L0–L4 → specs/baseline.md <!--ops:b1b2-->
- [x] M1/O2+O3: перепись парка + реестр → ~/vault/01-Projects/_park-inventory.md + _registry.yaml <!--ops:o2o3-->
- [x] git репо создан (PRIVATE Zira777ru/claude-ops-bot), гигиена секретов <!--ops:gitinit-->
- [x] кокпит: MD-таблицы в Specs/README/CLAUDE/Board (remark-gfm) <!--ops:gfm-->
