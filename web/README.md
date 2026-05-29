# Claude-Ops Web

Браузерный кокпит для управления проектами Claude-Ops.

## Команды

```bash
# Установить зависимости
npm install

# Сборка для продакшена (в web/dist/)
npm run build

# Запуск dev-сервера с прокси /api → localhost:8787
npm run dev

# Предпросмотр продакшен-сборки
npm run preview
```

## Стек

- Vite 5 + React 18 + TypeScript (strict)
- react-markdown — рендер CLAUDE.md и спецификаций
- Один CSS файл (styles.css) — тёмная тема, без UI-китов
- Аутентификация через cookie-сессию (credentials: 'include')

## Структура

```
src/
  main.tsx          — точка входа
  App.tsx           — корневой компонент, auth-стейт
  api.ts            — все API-вызовы
  types.ts          — TypeScript-типы
  styles.css        — тёмная тема
  components/
    LoginScreen.tsx — экран логина
    Sidebar.tsx     — список проектов
    ProjectView.tsx — шапка + табы проекта
    HealthDot.tsx   — индикатор здоровья
    Spinner.tsx     — спиннер загрузки
  tabs/
    OverviewTab.tsx — обзор проекта
    ClaudeMdTab.tsx — CLAUDE.md (markdown)
    SpecsTab.tsx    — спецификации (markdown)
    ActivityTab.tsx — лог активности
```

## API Backend

Dev-сервер проксирует `/api` → `http://localhost:8787` (webapp.py).
В продакшене статика раздаётся через aiohttp напрямую.
