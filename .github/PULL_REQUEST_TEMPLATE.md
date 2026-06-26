## What

Briefly describe the change and why.

## How to test

Steps a reviewer can follow to verify.

## Checklist

- [ ] Tests pass locally: `env -u WEB_COOKIE_SECURE venv/bin/python -m pytest tests/ -q`
- [ ] Frontend builds (if `web/` changed): `cd web && npm run build`
- [ ] No secrets, personal paths, or infra identifiers added to tracked files
- [ ] New code, comments, and UI strings are in English
