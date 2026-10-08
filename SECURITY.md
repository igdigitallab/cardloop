# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue or pull request.

- **GitHub private vulnerability reporting (preferred):**
  [Report a vulnerability](https://github.com/igdigitallab/cardloop/security/advisories/new)
  (repository → Security → Report a vulnerability). The report stays private between you and
  the maintainers until a fix ships.
- **Email:** [security@igdigi.com](mailto:security@igdigi.com)
- Publisher security page: <https://igdigi.com/security>

Please include: affected version or commit, reproduction steps, and impact. Reports are read
in English.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement that the report was received | within **5 business days** |
| Initial assessment (accepted / not a vulnerability / need more information) | within **14 days** of the report |
| Fix for a confirmed medium-or-higher severity issue (CVSS base score 4.0+) | released on `master` within **60 days** of the report |

Coordinated disclosure is appreciated: give us a reasonable window to ship a fix before any
public write-up. Once a fix is released we publish a
[GitHub Security Advisory](https://github.com/igdigitallab/cardloop/security/advisories) for
confirmed vulnerabilities, credit the reporter if they want that, and request a CVE where one
applies. Fixed vulnerabilities are also called out in the release notes
([CHANGELOG.md](CHANGELOG.md)).

## Scope and threat model

Cardloop is a **single-operator self-hosted tool** that runs Claude agents with **full host
access by design** (`bypassPermissions` — agents edit files, run git, and deploy without
per-action prompts). Read the [Security model](README.md#security-model) section before exposing
it to any network.

In scope:
- Authentication / session bypass (web password + optional TOTP).
- Login rate-limit / IP-trust bypass (`TRUSTED_PROXIES`).
- WebSocket Origin check bypass (terminal / browser pane upgrade from a foreign origin).
- Path traversal in the file/project APIs.
- Secret-vault disclosure beyond an authenticated session.
- Command injection via configurable commands (e.g. `log_cmd`).

Explicitly **out of scope** (these are by-design, documented behaviours, not bugs):
- An authenticated operator can run arbitrary work and read the decrypted vault — that is the
  product. The trust boundary is "authenticated operator," not "sandboxed agent."
- Exposing the cockpit without HTTPS / behind no auth — that's a deployment mistake; set
  `WEB_COOKIE_SECURE=true` and put it behind a reverse proxy.
- Reports from automated scanners that show no demonstrable impact on a default install.

## Cryptography notes

What Cardloop uses, all from the [`cryptography`](https://cryptography.io) package or the Python
standard library — nothing is implemented by hand:

- **Secret vault:** Fernet (AES-128-CBC + HMAC-SHA256, authenticated), key generated with
  `Fernet.generate_key()` and stored `chmod 600`.
- **Cockpit login:** the operator's password is turned into a session token with `scrypt`
  (n=2^14, r=8, p=1, 32-byte output) and a random per-installation salt. A salt set in
  `WEB_COOKIE_SALT` is used as given; a blank value, or a `CHANGE_ME...` placeholder such as the
  one an old `.env.example` shipped, is ignored and replaced by a random salt generated on first
  start and kept in `data/cookie_salt` (mode 0600, never printed or logged). Tokens are compared
  in constant time. There is a single operator and no user
  table, so no password hashes are stored; the password itself lives in the operator's own
  `.env`.
- **TOTP (optional 2FA):** HMAC-SHA1 as specified by RFC 6238, because authenticator apps
  require it. HMAC-SHA1 has no known practical weakness (the SHA-1 collision attacks do not
  apply to HMAC); this is the only place SHA-1 is used for a security purpose. Elsewhere SHA-1
  only makes short non-security fingerprints (dedupe keys).
- **Randomness:** salts, ids and tokens come from the `secrets` module (CSPRNG). `random` is
  used only for retry jitter.
- **TLS:** terminated by your reverse proxy or tunnel, not by Cardloop.

## Two-factor login: failure behaviour and break-glass

2FA is on exactly when an active TOTP secret is stored in the vault. The login handler tells
"not enrolled" apart from "cannot tell": if the vault answers that there is no secret, the password
alone logs in (nothing is enrolled yet); if the vault cannot be read at all (lost or wrong key,
corrupt store) the login **fails closed** — `503 {"error": "2fa_state_unreadable"}`, a
`[auth] login refused: 2FA state unreadable` line in the journal, and the attempt counts against
the login rate limit. The same holds for reading the recovery-code hashes and for saving a consumed
recovery code (a code that cannot be marked used is not accepted).

Break-glass, from a shell on the host (the operator already has one; nothing here weakens that):

- the vault key or store is damaged: restore the key file (`CLAUDE_OPS_SECRET_KEYFILE`,
  `CLAUDE_OPS_SECRET_KEY`) or the store, and log in as usual; or
- you accept losing the vault contents and want 2FA off: `secret rm __totp_secret__` (works only if
  the vault is readable — otherwise move the unreadable store (`data/vault/secrets.enc` by default) aside, which empties the vault),
  then log in with the password alone and re-enrol under Settings.

## Secrets in the environment and in /proc

The cockpit gets its secrets (`WEB_PASSWORD`, `WEB_COOKIE_SALT`, `BOT_TOKEN`, `COOLIFY_API_TOKEN`,
`AZURE_FOUNDRY_KEY`, `N8N_API_KEY`, `TWOCAPTCHA_API_KEY`, `OLLAMA_AUTH_TOKEN`,
`CLAUDE_OPS_SECRET_KEY`, `JOURNAL_TG_BOT_TOKEN`, and any variable named `*_PASSWORD`, `*_SALT`,
`*_TOKEN`, `*_SECRET` or `*_API_KEY`) from `.env`, or from the service's `EnvironmentFile=`. Left in
the environment they leak by accident through two channels, and two measures close them
([`runtime_secrets.py`](runtime_secrets.py), applied once at start by `bot.py`):

1. **Scrub.** Right after the environment is loaded, those variables are moved out of `os.environ`
   into a private in-process snapshot. In-process code reads them from the snapshot; nothing the
   cockpit spawns (the Claude CLI, Codex, Grok, terminals, test runners) inherits them, so an agent
   running `env` or `printenv` no longer puts the web password into its transcript. Not scrubbed:
   `ANTHROPIC_*` (handled by `CLAUDE_AUTH_MODE`) and `CLAUDE_CODE_OAUTH_TOKEN` (the Claude CLI's own
   credential), and any plain configuration. `AGENT_ENV_PASSTHROUGH=NAME,NAME` in `.env` keeps the
   names you want children to see (for example a `GITHUB_TOKEN` for `gh`).
2. **Non-dumpable process.** `prctl(PR_SET_DUMPABLE, 0)` is the first thing `bot.py` does. Under
   systemd, `EnvironmentFile=` puts the values into the process's initial environment block, which
   unsetting a variable in Python does not clear; that block is what `/proc/<pid>/environ` shows to
   every process of the same user. A non-dumpable process makes `/proc/<pid>/environ`, `mem`,
   `maps`, `fd/` and `cwd` root-owned and refuses a ptrace attach from a same-user process. Children
   are dumpable again after `exec`, so agents, shells and the OOM shield behave as before. Linux
   only; if the call fails the cockpit logs one `[security] WARNING` line and starts anyway.
   `make doctor` shows the result as "Process hardening" for the running service.

What this is **not**:

- Not a boundary against the agents the product runs. Claude and Codex agents run as the same user
  with full host access by design (see the threat model above) and can still read `.env`,
  `~/.claude/.credentials.json` and the vault key file from disk. The scrub removes the accidental
  channel (a dumped environment in a transcript or a log, a child that inherits a token it never
  needed), not a deliberate read.
- For Grok it closes a channel the file deny list could not: its sandbox mounts a host procfs, so
  a shell inside it could open `/proc/<cockpit pid>/environ`; for a non-dumpable process that open
  is refused. The deny list still has to cover `.env` and the data directory on disk.
- Root can read everything, and a forked child that has not yet called `exec` is still
  non-dumpable. Side effects of the non-dumpable flag: no core dump of the cockpit, and attaching
  `py-spy`, `gdb` or `strace` to it needs `sudo`.
- If the vault key lives only in the `CLAUDE_OPS_SECRET_KEY` variable (no key file), the `secret`
  CLI an agent runs no longer finds it; add the name to `AGENT_ENV_PASSTHROUGH` or use the key file.

Operator note (host setting, not changed by Cardloop): set `kernel.yama.ptrace_scope=1`
(`sysctl -w kernel.yama.ptrace_scope=1`, persisted in `/etc/sysctl.d/`). With `0`, which some
distributions ship by default, any process may ptrace any other process of the same user, so one
agent could read the memory of another agent's CLI or of any other service running as that user; `1`
limits ptrace to a process's own descendants. `make doctor` prints the current value.

## Supported versions

This is a young project; security fixes land on `master` and, from there, in the next tagged
release. Run the latest commit or the latest release tag.

## Our own hygiene

The repository is checked continuously: [CodeQL](.github/workflows/codeql.yml) static analysis,
[OpenSSF Scorecard](.github/workflows/scorecard.yml), Dependabot version and security updates
([dependabot.yml](.github/dependabot.yml)), GitHub secret scanning, and the automated test
suite in CI. Workflow actions are pinned to commit SHAs and run with a read-only token.
