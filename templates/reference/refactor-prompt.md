<!-- ORIGIN: ~/vault/03-Resources/_templates/refactor-prompt.md -->
<!-- Синхронизированная копия. При правке шаблона в vault → обновить здесь. -->
<!-- При правке здесь → синхронизировать обратно в vault, иначе drift. -->

# Refactor Prompt — план рефакторинга проекта

Запускается **после** аудита (audit-<date>.md уже создан) и **после** того как baseline покрыт (есть error alerting + тесты на critical paths). Без этого — STOP.

Промпт для копипасты в новую Claude Code сессию. Замени `<PROJECT>` на имя проекта.

---

## Промпт

```
# Refactor задача: <PROJECT>

Проект: $HOME/<PROJECT>/
Прочитай:
1. CLAUDE.md проекта
2. $VAULT/01-Projects/<PROJECT>/README.md
3. $VAULT/01-Projects/<PROJECT>/audit-*.md — последний audit-отчёт (если несколько — самый свежий)
4. $VAULT/03-Resources/_templates/project-baseline.md
5. Раздел "Tech gotchas" в $HOME/CLAUDE.md

Используй skill `legacy-modernizer` принципы (если установлен) — strangler fig, characterization tests, incremental migration.

## Режим: PLAN ONLY на первом проходе
НЕ менять код. Создать план рефакторинга → spec в `$VAULT/01-Projects/<PROJECT>/specs/`. Дальше — оператор читает, апрувит, начинаем по фазам.

---

## Этап 0: Pre-flight gate (БЛОКЕРЫ)

Прежде чем планировать рефакторинг — проверь что выполнены условия:

1. **Audit-отчёт существует и свежий** (<30 дней). Если нет → STOP, "сначала аудит: запусти audit-prompt"
2. **Все P0 из audit зафиксированы** (нет открытых критичных дыр). Если есть → STOP, "сначала P0 фиксы"
3. **Baseline покрыт:**
   - Error→Claude alerting работает (не просто есть в коде — реально шлёт алёрты, проверить логи)
   - Тесты на critical paths существуют и зелёные (`pytest` проходит)
   - `.env.example` актуальный
4. **Coverage critical paths ≥ 80%** (по списку из CLAUDE.md проекта). Если меньше → не STOP, но первая фаза рефакторинга = "дописать characterization tests"

Если хоть один блокер не выполнен — доложи в чате что блокирует, предложи что сделать первым.

---

## Этап 1: Scope определения

Что именно рефакторить — определи по аудит-отчёту:
- **P1 findings которые упираются в архитектуру** → главные кандидаты на рефакторинг
- **P2 findings которые накопились** → второстепенные
- **Homegrown solutions где есть готовое** (например, `requests`+`BeautifulSoup` → Firecrawl)
- **Зоны где часто фиксят баги** (смотри `git log --since="3 months ago" --oneline | grep -iE "fix|bug"`)

Не делай "рефакторинг всего проекта" — это big bang anti-pattern. Делай **зональный**: одна подсистема за раз.

Список зон в порядке приоритета (топ 3-5), каждая зона:
- **Имя** (например, "Form validation pipeline")
- **Файлы** (конкретные пути)
- **Что не так** (1-2 предложения)
- **Что должно быть** (target state)
- **Размер**: S (1-2 дня) / M (3-7 дней) / L (>7 дней)
- **Риск**: low / medium / high (зависит от того насколько hot path)

---

## Этап 2: Characterization tests (golden master) для каждой зоны

Перед изменением кода — захватить текущее поведение тестами.

Принцип из `legacy-modernizer/SKILL.md`: характеризационные тесты документируют **существующее** поведение (включая баги), а не идеальное. Их задача — поймать регрессию, не валидировать корректность.

Для каждой зоны:
- Минимум 5-10 тестов покрывающих happy path + edge cases
- Тесты должны быть **зелёные на текущем коде** (это базовая линия)
- Не моки внутренних компонентов зоны — только внешние границы (Telegram API, БД, файловая система)
- Сохранять снимки выводов (golden master) если функция возвращает сложные структуры

Если зона большая (L) — характеризация может занять отдельную фазу. Это нормально.

---

## Этап 3: Strangler Fig план

Для каждой зоны — incremental migration через facade + feature flag:

```python
# Pseudo
USE_NEW_X = os.getenv("USE_NEW_X", "false").lower() == "true"

def do_x(args):
    if USE_NEW_X:
        return new_implementation.do_x(args)
    return legacy_implementation.do_x(args)
```

Фазы для каждой зоны:
1. **Build new in parallel** — новая реализация рядом с legacy, под feature flag (default off)
2. **Shadow mode** — feature flag включает new, но результат сравнивается с legacy и логируется divergence (не используется в проде)
3. **Gradual rollout** — 10% → 25% → 50% → 100% по доле трафика/юзеров
4. **Cleanup** — после недели на 100% без алёртов — удалить legacy + feature flag

Для каждой фазы:
- **Rollback trigger** (что заставит откатиться): новые ошибки в логах, latency growth, юзер-жалобы
- **Validation** (что проверяем перед следующей фазой): error rate < baseline, ключевые метрики стабильны
- **Owner** — кто следит (оператор и/или мониторинг)

Если зона маленькая (S) и риск low — Strangler можно упростить до "новая ветка → тесты зелёные → merge → деплой → 24ч мониторинг → cleanup".

---

## Этап 4: Что НЕ делать (явные anti-patterns)

- ❌ **Big bang rewrite** — переписать весь модуль и заменить разом
- ❌ **Рефакторить вокруг бага** — нашёл баг, "заодно" причесал соседние 10 файлов
- ❌ **Менять API/контракты под видом рефакторинга** — если меняется поведение, это редизайн, не рефакторинг
- ❌ **Рефакторить hot path без feature flag** — даже зелёные тесты не страхуют от прод-нагрузки
- ❌ **Удалять legacy до 100% rollout стабильно неделю** — нужен откат
- ❌ **Молча менять зависимости** — bump версий = отдельный PR с тестами

---

## Этап 5: Spec файл

Создай `$VAULT/01-Projects/<PROJECT>/specs/<NNNN>-refactor-<YYYY-MM-DD>.md`:

\```markdown
# Refactor Plan — <PROJECT> — <YYYY-MM-DD>

## Pre-flight
- [x] Audit: <audit-file>
- [x] Baseline OK
- [x] Coverage critical paths: <%>

## Scope — Top N зон
### Zone 1: <Name>
- **Files:** path/to/file.py, path/to/other.py
- **Что не так:** ...
- **Target:** ...
- **Size:** S/M/L
- **Risk:** low/medium/high

### Zone 2: ...

## Phase plan

### Phase 1: Characterization tests
- [ ] Tests for Zone 1 (target: 10 tests)
- [ ] Tests for Zone 2 (target: 8 tests)
- **Exit criteria:** все green на текущем коде

### Phase 2: Build new in parallel — Zone 1
- [ ] Feature flag `USE_NEW_<ZONE1>` создан
- [ ] New implementation `<file>` написана
- [ ] Все characterization tests green с feature flag=true
- **Exit criteria:** divergence в shadow mode < 0.1%

### Phase 3: Gradual rollout — Zone 1
- [ ] 10% → 24h мониторинг
- [ ] 25% → 24h
- [ ] 50% → 48h
- [ ] 100% → 7 days
- **Rollback triggers:** error rate >2x baseline, новые типы ошибок в логах, юзер-жалобы
- **Exit criteria:** 7 days на 100% без инцидентов

### Phase 4: Cleanup — Zone 1
- [ ] Удалить legacy
- [ ] Удалить feature flag
- [ ] Обновить README + CLAUDE.md проекта

### Phase 5+: Zone 2, Zone 3, ...

## Estimated timeline
- Phase 1: X дней
- Phase 2: Y дней
- ...
- Total: Z недель

## Что НЕ входит в этот рефакторинг
(Явно перечислить — что в backlog, в другой spec, или не делаем вовсе)
\```

После создания spec — доложи кратко:
"Refactor plan готов: vault/01-Projects/<PROJECT>/specs/<NNNN>-refactor-<date>.md. N зон. Total: Z недель. Старт — Phase 1 (characterization tests)."

---

## После плана

Оператор читает spec, при апруве — отдельная сессия на каждую фазу:
- "Сделай Phase 1 из spec <NNNN>" — Claude пишет characterization tests
- "Сделай Phase 2 zone 1" — Claude строит parallel implementation
- и т.д.

Каждая фаза — отдельная сессия с чистым контекстом + ссылка на spec.
```

---

## Связанные шаблоны

- [[audit-prompt]] — обязательное предусловие
- [[project-baseline]] — должен быть зелёным
- [[triage-prompt]] — ранжирование всех проектов
