# Cardloop Web

Browser cockpit for managing Cardloop projects.

## Commands

```bash
# Install dependencies
npm install

# Production build (output: web/dist/)
npm run build

# Dev server with /api proxy → localhost:8787
npm run dev

# Preview production build
npm run preview
```

## Stack

- Vite 5 + React 18 + TypeScript (strict)
- react-markdown — renders CLAUDE.md and specs
- Single CSS file (styles.css) — dark theme, no UI kit
- Authentication via cookie session (credentials: 'include')

## Structure

```
src/
  main.tsx          — entry point
  App.tsx           — root component, auth state
  api.ts            — all API calls
  types.ts          — TypeScript types
  styles.css        — dark theme
  components/
    LoginScreen.tsx — login screen
    Sidebar.tsx     — project list
    ProjectView.tsx — header + project tabs
    HealthDot.tsx   — health indicator
    Spinner.tsx     — loading spinner
  tabs/
    OverviewTab.tsx — project overview
    ClaudeMdTab.tsx — CLAUDE.md (markdown)
    SpecsTab.tsx    — specs (markdown)
    ActivityTab.tsx — activity log
```

## API Backend

The dev server proxies `/api` → `http://localhost:8787` (webapp.py).
In production, static files are served directly by aiohttp.
