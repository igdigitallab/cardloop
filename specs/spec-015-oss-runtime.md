---
created: 2026-06-04
status: in-progress
---

# Spec 015 — OSS runtime: optional Telegram, configurable web, English-only

> First spec written in English on purpose — English is now the project language
> (see Part 4). Builds on [[spec-014-oss-hardening]]. Igor's live instance MUST keep
> working bit-for-bit (BOT_TOKEN set, subscription auth, RESPONSE_LANGUAGE=по-русски).

## Goal
Let anyone self-host claude-ops-bot with the minimum: Claude auth + web password + a
port. Telegram and a domain become optional. English becomes the project language.

## Principle
**The web cockpit is the product. Telegram is an optional transport.** Minimal run =
Claude auth + `WEB_PASSWORD` + `host:port`. Everything else (TG, domain/TLS, `gh`,
systemd, vault) is an optional layer.

Target minimal run:
```
CLAUDE_AUTH_MODE=subscription   # or api_key
WEB_PASSWORD=...
WEB_HOST=127.0.0.1  WEB_PORT=8787
→ open http://localhost:8787
```

## Part 1 — Auth source (subscription | api_key)
Today the code forces the Claude subscription and hard-`pop`s `ANTHROPIC_API_KEY`
(a money-safety guard). Make the source a setting:
- `CLAUDE_AUTH_MODE = subscription` (default) | `api_key`.
- `subscription`: current behavior; optional `CLAUDE_CREDENTIALS_PATH`
  (default `~/.claude/.credentials.json`).
- `api_key`: set `ANTHROPIC_API_KEY` from config, do NOT pop it, SDK bills the API.
- ⚠️ **Safety:** default STAYS `subscription` (no accidental API billing). `api_key`
  is an explicit opt-in. This is the only money-sensitive change — design is
  "safe default + conscious opt-in", never the reverse.

## Part 2 — Optional Telegram (invert the entrypoint)
Today the process can't start without `BOT_TOKEN` — `main()` is PTB-first
(`ApplicationBuilder().token(BOT_TOKEN)…run_polling()`), webapp lives in PTB
`post_init` (`_on_start`). Refactor:
- `main()` always builds `ctx` + starts the web cockpit + engine on an asyncio loop.
- PTB starts **only if** `BOT_TOKEN` is set (manual lifecycle:
  `initialize()/start()/updater.start_polling()`), else web-only with `ptb_app=None`.
- Guard every TG side-effect on `ptb_app is None` → no-op: `_run_card` pings,
  self-heal pings, `notify_on_error`, watchdog interrupt messages.
- Web-only loses nothing: projects are created/bound via the cockpit UI (+New project,
  file browser); forum-topic auto-binding is TG-only and simply absent.

## Part 3 — Configurable web + cookie fix
- `WEB_HOST` env, default `127.0.0.1` (safe; not exposed to LAN/internet unless chosen).
  Document `0.0.0.0` for LAN. `WEB_PORT` already configurable.
- `WEB_COOKIE_SECURE` env — fixes the silent http-login trap: `secure=True` is
  hardcoded (webapp.py) and a browser will NOT store a secure cookie over plain
  `http://<LAN-IP>:PORT` → login fails with no error. Default relaxed for local http,
  `true` behind an HTTPS proxy.
- Domain is NOT required: the app serves at `localhost:PORT`. A domain = optional remote
  access via reverse proxy (Caddy/nginx) or tunnel (Cloudflare Tunnel). Ship example
  configs in docs.

## Part 4 — English-only
English is the project language. The agent's **reply** language stays controlled by
`RESPONSE_LANGUAGE` (Igor keeps `по-русски` → his agent answers Russian even though the
nudge/UI/logs are English).
- **UI** (`web/src/`): make `i18n/en.ts` the default; translate `ru.ts` strings; fix
  hardcoded Russian in `.tsx` components (e.g. BoardTab).
- **Backend**: `bot.py` TG-facing messages + `TELEGRAM_NUDGE` + log/print;
  `webapp.py` log/print + agent-prompt templates (audit / card-completion / upgrade
  prompts) → English. (API error JSON is already English.)
- **Comments + docstrings** (`.py` + `.ts`) → English.
- **Docs**: README + ARCHITECTURE → English; new specs in English. CLAUDE.md → add the
  English-only rule (full translation may follow).
- **Tests**: update assertions that match Russian strings. `pytest` is the safety net
  for backend string changes.

## Runtime preservation (Igor's instance — verify before any restart)
Set in Igor's gitignored `.env` so behavior is 1:1:
`CLAUDE_AUTH_MODE=subscription`, `WEB_HOST=0.0.0.0`, `WEB_COOKIE_SECURE=true`,
`RESPONSE_LANGUAGE=по-русски` (already set), `BOT_TOKEN`/`ALLOWED_USERS` (already set).
Verify: `import bot, webapp`; start in web-only mode (no token) AND with token; pytest;
web build. Only then restart.

## Phases
- **A (runtime):** Parts 1-3. Runtime-safe. Verify start in both modes.
- **B (English):** Part 4. Partitioned by file set (UI / bot.py / webapp.py / docs),
  pytest + build gated.
- **C (docs):** example reverse-proxy/tunnel configs; README quickstart with two paths
  (web-only / with-Telegram).

## Non-goals
- Per-user billing / multi-user → [[spec-013-multi-user]].
- Non-Telegram transports (Discord/Slack/Matrix) → separate spec.
- Runtime UI language switcher — English default is enough for v1; `ru.ts` may remain as
  an optional locale.

## Related
- [[spec-014-oss-hardening]] · [[spec-013-multi-user]] · [[spec-004-oss-release]].
