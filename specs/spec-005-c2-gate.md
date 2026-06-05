# Spec C2 — Гейт «Применить / Отмена» + worktree-per-task

> Статус: APPROVED Игорем 2026-05-31. Реализация на ветке `feature-c2-gate`.
> Карточка доски: `ops:1a4662`.

## Проблема (сейчас)
`_run_card` запускает агента с `cwd = project["cwd"]` — агент пишет **прямо в рабочее дерево проекта**. Колонка Review = только показ git-diff (сайдкар `data/runs/<id>.md`). Отката нет: изменения уже в файлах, «Отмена» невозможна. Это блокирует автономию (СЕВЕР): нельзя дать агенту прогон без риска.

## Решение
Прогон карточки изолируется в **git worktree** на ветке `card-<id>`. Рабочее дерево проекта не трогается. В Review пользователь видит diff и решает:
- **Применить** → `git merge --no-ff card-<id>` в основную ветку проекта + удалить worktree + ветку.
- **Отмена** → удалить worktree + ветку `card-<id>`. Основное дерево не изменилось.

## Решения Игоря (зафиксированы, не менять)
1. **Apply = merge --no-ff** ветки `card-<id>` (1 merge-коммит на карточку, история видна, откат через git).
2. **Не-git проект ИЛИ dirty-дерево → ДЕГРАДАЦИЯ к текущему поведению** (прогон прямо в cwd, без изоляции). Git-init НЕ делаем автоматически. В Review — баннер «без worktree-отката». При apply dirty-дерева — merge проверяется на конфликт, при конфликте НЕ применяем, сообщаем.

## Архитектура

### Когда применяется worktree-режим
В `api_move_task` при `to == "in_progress"`, ПЕРЕД запуском `_run_card`:
- `_git_info(cwd)` ≠ None (проект под git) **И** дерево чистое (`git status --porcelain` пусто) → **worktree-режим**.
- Иначе → **legacy-режим** (текущее поведение, прогон в cwd). Флаг прокинуть в `_run_card`.

### Worktree-режим: жизненный цикл
1. **Setup** (в `api_move_task` или начале `_run_card`, под board-lock не держать долго):
   - `base_branch = git rev-parse --abbrev-ref HEAD` (запомнить — для merge назад).
   - `wt_path = <cwd>/.worktrees/card-<id>` (`.worktrees/` уже в .gitignore проектов — проверить, для не-наших проектов добавить в exclude локально через `git worktree` и не коммитить).
   - `git worktree add <wt_path> -b card-<id>` (новая ветка от HEAD).
   - Если worktree уже существует (повторный прогон) — сначала очистить: `git worktree remove --force` + `git branch -D card-<id>`.
2. **Прогон**: `run_engine(cwd=wt_path, ...)` — агент пишет в worktree. Остальное (`_bus_publish`, сайдкар, sessions) как сейчас.
3. **После прогона**:
   - `_git_diff_card(wt_path)` — diff берём из worktree (vs base через `git diff <base_branch>...HEAD` или просто committed+uncommitted в ветке).
   - **Авто-коммит в ветке**: если в worktree есть изменения — `git -C <wt_path> add -A && git commit -m "card <id>: <prompt 60 симв>"`. Иначе ветка пустая (агент ничего не сделал) — пометить.
   - Сайдкар (`_write_sidecar`) дополнить полями: `mode: worktree|legacy`, `branch: card-<id>`, `base_branch`, `wt_path`, `has_changes: bool`.
   - Карточка → **Review** (ok) / **Failed** (err) — как сейчас.
   - Worktree НЕ удаляем (нужен для diff в Review). Ветка остаётся.
4. **Гейт (новые эндпоинты)**:
   - `POST /api/projects/{id}/tasks/{card}/apply`:
     - Прочитать meta из сайдкара (branch, base_branch, wt_path).
     - В основном дереве: `git checkout <base_branch>` (если надо), `git merge --no-ff card-<id> -m "Применить карточку <id>: <prompt>"`.
     - При конфликте merge → `git merge --abort`, вернуть 409 `{"error":"merge conflict","detail":...}`, карточку НЕ двигать, worktree НЕ удалять.
     - Успех → `git worktree remove --force <wt_path>` + `git branch -d card-<id>` (ветка уже смержена). Карточка Review → **Done** (в DONE.md, как `to=done`). Сайдкар обновить `applied: true`.
   - `POST /api/projects/{id}/tasks/{card}/discard`:
     - `git worktree remove --force <wt_path>` + `git branch -D card-<id>`.
     - Карточка Review → **Backlog** (вернуть в работу) ИЛИ удалить — вернуть в Backlog (безопаснее, не теряем формулировку). Сайдкар обновить `discarded: true`.
   - Оба эндпоинта: `_valid_card_id`, board-lock на запись карточки, проверка что проект под git и worktree существует (иначе 400 «нечего применять — legacy-режим»).

### Legacy-режим (не-git / dirty)
- Всё как сейчас: прогон в cwd, перенос в Review/Failed, сайдкар с `mode: legacy`.
- Эндпоинты apply/discard для такой карточки → 400 «карточка выполнена в рабочем дереве, гейт недоступен; изменения уже применены».

## Фронтенд (BoardTab.tsx + api.ts)
- Карточка в Review с `mode: worktree` и `has_changes`: показать 2 кнопки **✓ Применить** / **✗ Отмена** (рядом с существующей 📄 «результат»).
- `mode: legacy` ИЛИ `has_changes=false`: кнопок гейта нет, баннер «изменения в рабочем дереве» / «нет изменений».
- `api.ts`: `applyCard(id, card)` → POST .../apply; `discardCard(id, card)` → POST .../discard.
- Конфликт merge (409) → Toast с текстом ошибки, карточка остаётся в Review.
- ARIA: `aria-label` на кнопках, `role="dialog"` если будет подтверждение discard (discard необратим → ConfirmModal «Отменить изменения карточки? Ветка будет удалена.»).
- i18n-строки в `web/src/i18n/ru.ts`.

## Безопасность / краевые случаи
- `_valid_card_id` на всех новых путях (anti-injection — уже есть паттерн).
- Имя ветки `card-<id>` — id уже валидирован regex `[a-f0-9-]{4,20}`, безопасно для git.
- worktree-операции через `asyncio.create_subprocess_exec` (НЕ shell) — как `_git_cmd`.
- Замок `running[session_key]` — снимается в `finally` `_run_card` как сейчас (worktree-режим не меняет логику замка).
- Если бот рестартует посреди прогона — worktree остаётся на диске (orphan). Добавить: при старте webapp ИЛИ в apply/discard — толерантность к отсутствию worktree (404-safe). Опц.: эндпоинт/кнопка «прибрать orphan worktrees» — НЕ в этой итерации, отметить в Backlog.
- Merge назад в `base_branch`: если основное дерево стало dirty между прогоном и apply → merge может упасть; ловить, 409, не ломать.

## Тесты (tests/test_c2_gate.py)
- Worktree setup/teardown на временном git-репо (фикстура tmp git project).
- Режим-детектор: git+clean → worktree; не-git → legacy; git+dirty → legacy.
- apply: merge --no-ff успешный → коммит в base, worktree удалён, карточка в DONE.
- apply при конфликте → 409, merge --abort, карточка в Review, worktree жив.
- discard → ветка/worktree удалены, карточка в Backlog.
- legacy-карточка → apply/discard возвращают 400.
- `_valid_card_id` на новых эндпоинтах (bad id → 400).
- Все существующие тесты (300) остаются зелёными.

## Файлы
- `webapp.py`: `_card_worktree_setup`, `_card_worktree_diff`, `_commit_in_worktree`, `api_card_apply`, `api_card_discard`; правки `_run_card` (параметр режима), `api_move_task` (детектор режима), `_write_sidecar` (meta-поля), 2 роута в `start()`.
- `web/src/tabs/BoardTab.tsx`, `web/src/api.ts`, `web/src/types.ts`, `web/src/i18n/ru.ts`.
- `tests/test_c2_gate.py`.
- `docs/API.md`: +2 эндпоинта. `CLAUDE.md`: gotcha про worktree-режим карточек. `ARCHITECTURE.md`: обновить поток задачи.

## Out of scope (этой итерации)
- Уборка orphan-worktrees после краша (→ Backlog).
- Авто-git-init для не-git проектов (отклонено оператором).
- Squash-режим (принято: merge --no-ff).
