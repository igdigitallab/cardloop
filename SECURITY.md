# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue or PR.

- Open a [GitHub Security Advisory](https://github.com/cardloop/cardloop/security/advisories/new)
  (Security → Report a vulnerability), or
- email the maintainers (see the repository profile).

Please include: affected version/commit, reproduction steps, and impact. We aim to acknowledge
within a few days. Coordinated disclosure is appreciated — give us a reasonable window to ship a
fix before any public write-up.

## Scope and threat model

Cardloop is a **single-operator self-hosted tool** that runs Claude agents with **full host
access by design** (`bypassPermissions` — agents edit files, run git, and deploy without
per-action prompts). Read the [Security model](README.md#security-model) section before exposing
it to any network.

In scope:
- Authentication / session bypass (web password + optional TOTP; Telegram `ALLOWED_USERS`).
- Login rate-limit / IP-trust bypass (`TRUSTED_PROXIES`).
- Path traversal in the file/project APIs.
- Secret-vault disclosure beyond an authenticated session.
- Command injection via configurable commands (e.g. `log_cmd`).

Explicitly **out of scope** (these are by-design, documented behaviours, not bugs):
- An authenticated operator can run arbitrary work and read the decrypted vault — that is the
  product. The trust boundary is "authenticated operator," not "sandboxed agent."
- Exposing the cockpit without HTTPS / behind no auth — that's a deployment mistake; set
  `WEB_COOKIE_SECURE=true` and put it behind a reverse proxy.

## Supported versions

This is a young project; security fixes land on `master`. Run the latest commit.
