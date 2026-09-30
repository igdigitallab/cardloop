## What

Briefly describe the change and why.

## How to test

Steps a reviewer can follow to verify.

## Checklist

- [ ] Tests pass locally: `env -u WEB_COOKIE_SECURE venv/bin/python -m pytest tests/ -q`
- [ ] Tests added or updated for new functionality / the bug being fixed
- [ ] Lint passes: `venv/bin/ruff check .` (and `cd web && npm run lint` if `web/` changed)
- [ ] Frontend builds (if `web/` changed): `cd web && npm run build`
- [ ] No secrets, personal paths, or infra identifiers added to tracked files
- [ ] New code, comments, and UI strings are in English
