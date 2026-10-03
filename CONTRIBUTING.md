> CONTRIBUTING = how to set up the environment, run tests/lint, and commit. Code map → ARCHITECTURE.md. Working rules → CLAUDE.md.

# Contributing to Cardloop

## How to contribute

- **Report a bug or request a feature:** open a [GitHub issue](https://github.com/igdigitallab/cardloop/issues/new/choose)
  (there are templates for both). Search existing issues first; the issue tracker is the public,
  searchable archive of reports and answers. We aim to reply to every new issue within 14 days —
  the answer may be "no", or a question, but it will not be silence.
- **Report a security vulnerability:** *not* here — follow [SECURITY.md](SECURITY.md) (private
  reporting).
- **Send a change:** fork the repository, make the change on a branch, and open a **pull request
  against `master`**. A maintainer reviews it; small, focused pull requests are merged fastest.
  (The maintainers themselves commit straight to `master`; contributions from other people arrive
  as pull requests.)
- **Ask a question or discuss an idea:** open an issue and say it is a question — issues and pull
  request threads are the discussion channel (searchable, linkable, no account needed beyond
  GitHub).

### What an acceptable contribution looks like

- **Tests come with the change.** Major new functionality must add tests to the automated suite
  (`tests/` for Python — pytest; `web/src/**/*.test.ts` for frontend logic — node:test); a bug fix
  should add a regression test that fails without the fix. This is the project's test policy, and
  the [PR template](.github/PULL_REQUEST_TEMPLATE.md) asks for it.
- **CI must be green:** backend tests on Python 3.11 and 3.12, `ruff check .`, and the frontend's
  `npm run lint` + `npm run build` all run on every pull request (see
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Static analysis (CodeQL) runs on every
  push and pull request too.
- **Coding standard:** Python is linted by `ruff` (rule set in [`ruff.toml`](ruff.toml)); the
  frontend by ESLint + Prettier (`web/eslint.config.js`, `web/.prettierrc`), with zero warnings
  allowed.
- **English only:** code, comments, docstrings, log output, UI strings and docs are in English.
- **No secrets or personal data** in tracked files — no tokens, passwords, IPs, or `/home/<user>`
  paths (use `$HOME`, relative paths, or `.env` + a placeholder in `.env.example`).
- **User-visible change → a line in [CHANGELOG.md](CHANGELOG.md)** under `[Unreleased]`; that file
  is the human-readable release notes.
- **Commit messages** follow the style in [Commit style](#commit-style) below.

## Quick Start

One command does everything (venv + deps + .env + frontend build):

```bash
git clone https://github.com/igdigitallab/cardloop.git
cd cardloop
./install.sh            # or: make install
claude login            # one-time Claude subscription auth
# edit .env → set WEB_PASSWORD
venv/bin/python bot.py  # cockpit → http://localhost:8787
```

Prefer the manual steps? They are equivalent to what `install.sh` runs:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # runtime + dev
cp .env.example .env       # set WEB_PASSWORD; WEB_COOKIE_SALT auto-generates if blank
cd web && npm ci && npm run build && cd ..
venv/bin/python bot.py
```

> To update later: `./update.sh` (or `make update`).

## Auth note

Cardloop uses **subscription auth** via `~/.claude/.credentials.json` (claudeAiOauth).
Do **not** set `ANTHROPIC_API_KEY` — the engine explicitly removes it; setting it would
switch billing to API pay-per-token mode instead of using your Claude subscription.

## Tests

```bash
venv/bin/python -m pytest -q
# or
make test
```

CI runs the same command (`venv/bin/python -m pytest tests/ -q`) on Python 3.11 and 3.12. One
provider-routing test (`test_codex_chat_routes_thread_without_touching_claude_session`) assumes
the optional Codex provider flag is on, so CI sets `CODEX_ENABLED=true`; if you run without a
`.env`, do the same. Opt-in suites (browser e2e,
model-alias probes) are described in [CLAUDE.md](CLAUDE.md#operations).

### Grok provider tests

The default run needs **nothing** from Grok: no binary, no login, no network. The engine tests drive
a fake `grok` (`tests/fake_grok_acp.py`, a real subprocess speaking ACP) that replays recorded
fixtures — `tests/fixtures/grok/*.jsonl` (26 captured from the real CLI, plus hand-made
`synthetic_*` ones for misbehaviours) and `tests/fixtures/grok_history/` (scrubbed real session
files). `GROK_BIN` points at the fake, as `e2e_fake_engine` does for Claude.

| What | Command | Needs |
|---|---|---|
| Re-record the wire fixtures after a CLI bump | `venv/bin/python tools/grok_record_fixtures.py --list` / `--scenario all` / `--scenario NAME` | the `grok` binary and a login; it copies `auth.json` mode 600 into a throwaway `GROK_HOME`, scrubs ids/paths/emails/tokens, refuses a fixture that still holds a secret-looking value, and deletes the scratch tree |
| **After every CLI bump: the whole gate** | `tools/grok-verify check` (`--dry-run` first; `--login <GROK_HOME or auth.json>`; `--with-cockpit` adds the wired-cockpit live test) | runs `grok_live`, `grok_canary` and a fixture re-record diffed by event/field skeleton against `tests/fixtures/grok/`; prints the one-line `KNOWN_GOOD_VERSIONS` edit only when everything passed, never edits a file; exit 1 on any failure or skipped step |
| Long run | `tools/grok-verify soak` (`--duration 24h --interval 20m`; smoke: `--cycles 2 --interval 5`) | one tiny real turn per cycle under the production sandbox profile; asserts no leftover process, bounded litter and `GROK_HOME` growth; canary every 12th cycle; costs subscription quota |
| Real binary: isolation, sandbox, one real turn | `venv/bin/python -m pytest tests/test_grok_live.py -m grok_live` | `grok`, `bwrap`, a login in the cockpit's `GROK_HOME` (`tools/grok-acct login`); spends model turns on the subscription; skips cleanly when something is missing |
| Egress canary (a repo with an 8 MB blob in git history, one "reply OK" turn, fail above 1 MiB per connection) | `venv/bin/python -m pytest tests/test_grok_live.py -m grok_canary` | same, plus `ss`; run by hand after every CLI update and before enabling a new project |
| Grok against a real cockpit with the fake CLI | `venv/bin/python -m pytest tests/e2e/test_grok_*.py -m e2e` | `web/dist`, Playwright (see CLAUDE.md); `E2E_SHOTS_DIR=<dir>` also writes the visual-check screenshots |

`grok_live` and `grok_canary` are excluded from the default run in `pytest.ini`, exactly like `e2e`.
Never point either at a real repository or a login you care about: they run a model with a shell.
A guard on the Grok path gets a test that fails when the guard is removed (a line-anchored mutation
harness that refuses to run unless the unmodified code is green is how this wave was checked).

## Python lint

```bash
venv/bin/ruff check .    # rule set: ruff.toml — must report zero findings
```

## Troubleshooting

```bash
make doctor          # or: venv/bin/python tools/doctor.py
```

One-command diagnosis (< 5s, read-only, secrets always redacted): versions, auth, config, systemd
service health, runtime reachability, data counts, then a ✗/⚠ verdict with a remedy per finding.
Non-zero exit code when any ✗ is present. Run it before filing a bug report and paste its output
into the issue.

## Frontend lint & format

```bash
cd web
npm run lint      # ESLint check
npm run format    # Prettier format
npm run build     # Production build → web/dist/
```

After editing `web/`, always rebuild before testing or deploying.

## Commit style

```
type(scope): short description (ops:ID)
```

- **type:** `feat` | `fix` | `docs` | `refactor` | `test` | `chore`
- **scope:** `bot` | `webapp` | `web` | `docs` | `tests` | `ci`
- **ops:ID** — kanban card ID from `TASKS.md` (e.g. `ops:c05lic`)

Examples:
```
feat(webapp): add project rename endpoint (ops:s03rename)
fix(bot): retry on NetworkError in _tg_call (ops:b12retry)
docs: add API reference (ops:m12apidoc)
```

## Secrets

- Never commit `.env` (it is gitignored).
- Never hardcode tokens, passwords, or IPs in tracked files.
- Before adding a new file: verify it does not contain secrets or personal data.

## Project layout

```
bot.py          — web-only launcher (loads env/auth, builds ctx, starts the cockpit); engine lives in engine.py
webapp.py       — aiohttp cockpit, 57 HTTP routes, event bus
web/            — React + Vite SPA (build → web/dist/)
templates/      — new-project starters (*.tpl) + vault reference copies (reference/)
tests/          — pytest suite (3,500+ tests; run via venv/bin/python -m pytest)
data/           — runtime state (gitignored: topics.json, sessions.json, audit/, runs/)
docs/API.md     — HTTP API reference
docs/GROK.md    — Grok Build provider: operator runbook (enable, privacy, isolation, doctor)
tools/doctor.py — one-command cockpit diagnosis (make doctor)
tools/daily-journal.py — Haiku digest of the day's cockpit work → a vault Markdown note (README)
```

See ARCHITECTURE.md for a full code map with file:line references.
