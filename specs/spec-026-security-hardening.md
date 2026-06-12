# Spec 026 — Security Hardening: Secret Vault Backend + Cockpit Door

**Project:** claude-ops
**Date:** 2026-06-11
**Status:** [ ] Draft / [ ] Ready / [x] In progress / [ ] Done
**Progress:** Phase 0 deployed & smoke-tested (2026-06-11); Phase 1 firewall live + persistent (`claude-ops-firewall.service`); Phases 2 (TOTP) & 3 (vault) pending operator go.

---

## Context (why)

ClaudeOps is, by design, a **trusted server administrator**: every channel can drive an agent in `bypassPermissions`, so an authenticated operator already has full control of the host. The owner's explicit decision: **the assistant SHOULD be able to use keys/passwords/credentials** — hiding secrets *from Claude* is not a goal. The real goals are different:

1. **Secrets must not sit in plaintext at rest.** Today the operator's master credentials live in a plaintext file that leaks "for free" via backups, an accidental commit, or anyone who can read the disk. Move secrets behind an encrypted vault; resolve them only at the moment of use.
2. **The door must be stronger than one password.** Because the cockpit is the key to everything, the only meaningful defense against an *intruder* is to keep them out of the cockpit and to fix the brute-force weaknesses. Add a second factor and repair the rate-limiter and network exposure.

A full security audit (`~/vault/01-Projects/Claude-Ops-Bot/security-audit-2026-06-11.md`) produced the finding inventory behind this spec.

---

## Acceptance criteria

### Phase 0 — Quick wins (safe, reversible)
- [ ] Login rate-limiter keys on the **real client IP** (`CF-Connecting-IP`, else first `X-Forwarded-For`, else `req.remote`) — mirrors the incident endpoint, which already does this.
- [ ] A single shared proxy IP can **no longer lock out everyone**: replace the hard global block with a per-real-IP counter; on threshold, apply an increasing delay rather than a flat denial, and never count successful logins toward the limit.
- [ ] Startup **fails fast** (process exits with a clear message) if `WEB_PASSWORD` is empty/unset — no silent "blank password = full access".
- [ ] `.env` file mode is `0600` (operational; not world-readable).
- [ ] Existing tests stay green; new tests cover real-IP extraction, no-self-lockout, and empty-password fail-fast.

### Phase 1 — Network exposure (GATED — can cut cockpit access)
- [ ] The cockpit port is reachable **only via the Cloudflare tunnel path**, not from the LAN. Do **not** naively flip `WEB_HOST` to `127.0.0.1` — the tunnel/reverse-proxy reaches the host service over the Docker bridge, so a loopback bind would break access. Restrict at the **firewall** layer (allow the proxy/bridge source, deny the rest of the LAN), verifying tunnel reachability before and after.
- [ ] `WEB_COOKIE_SECURE=1` confirmed in the deployed environment.

### Phase 2 — Second factor on cockpit login (GATED — can lock out the operator)
- [ ] After a correct password, the cockpit requires a **TOTP** code (authenticator app). Optional WebAuthn/passkey is a later iteration, out of scope here.
- [ ] First-time enrollment flow produces a QR/secret and a set of **one-time recovery codes**.
- [ ] A documented **break-glass** path exists (disable TOTP from the host shell) so the operator can never be permanently locked out.
- [ ] TOTP secret and recovery codes are stored encrypted at rest (see Phase 3 backend), never in plaintext config.

### Phase 3 — Built-in encrypted secret store + Vault UI (revised 2026-06-11; replaces the VaultWarden direction)
**Decision:** NO external secret service. VaultWarden is the owner's PERSONAL password manager, not an infra dependency, and OSS users must never be forced to stand one up. Secrets live in a built-in encrypted-at-rest store with a thin CLI, usable identically from the terminal (Claude CLI) and the cockpit. Locked choices: master key = keyfile (0600); UI reveal = plain 👁 for now (tighten after Phase 2); migrate ALL ~50 genuine secrets; UI = a global "🔐 Vault" button.

- [ ] **Engine:** a single encrypted store file (gitignored), encrypted with `cryptography` Fernet (already a dependency — no new external dep, no service). Record shape: `name → {value, category, notes, updated_at}`. One shared module used by BOTH the CLI and the cockpit (the `_secrets_read` pattern).
- [ ] **Master key:** resolved from env `CLAUDE_OPS_SECRET_KEY` if set, else a keyfile (default `~/.config/claude-ops/secret.key`, chmod 600), path env-overridable. `secret init` generates the key (0600) if absent. The key is NEVER written into the encrypted file or any tracked file.
- [ ] **CLI `secret`:** `get NAME` / `set NAME [value]` / `list` / `rm NAME` / `init` / `import <file>`. Native read AND write for Claude via Bash; works in any terminal/project (on PATH via a console entry or documented symlink).
- [ ] **Cockpit API:** `GET /api/secrets` returns names + categories ONLY (never values); `GET /api/secrets/{name}` returns the decrypted value ON DEMAND (audited); `POST`/`PUT`/`DELETE` add/edit/remove. All under the auth middleware.
- [ ] **Cockpit UI:** a GLOBAL "🔐 Vault" view (peer to global Files / Schedules 🗓), reached from the global nav. Names grouped by category, values masked; per-row reveal (👁, on-demand single-secret decrypt — never bulk-dump to the page), copy, edit, delete; add-secret; search/filter.
- [ ] **Resolver:** repoint the Phase-3a resolver from `vault:`/`vw` to `secret:<name>` reading the built-in store. The `vw` dependency is dropped from this path. Plain (non-`secret:`) values still pass through unchanged; missing/failed lookups still fail loud.
- [ ] **Migration (GATED destructive):** `secret import` ingests the ~50 genuine secrets from the owner's plaintext credentials file (config/UUIDs/IPs/URLs are NOT secrets — they stay in docs); each verified retrievable via `secret get`; THEN, on explicit go, the plaintext secrets AND the VaultWarden master-password reference are removed from that file.

---

## What NOT to do
- Do **not** add a file-browser blocklist that hides `.ssh`/`.claude`/credentials from the agent's own runtime — the owner wants the assistant to use them, and the agent reads files through its own tools, not the HTTP file API. (See "Open decision" below for the narrower, optional HTTP-browser hardening.)
- Do **not** change the engine, channels, board, or any unrelated subsystem.
- Do **not** flip the network bind without the firewall verification (Phase 1 gate).
- Do **not** delete the plaintext credentials file until vault retrieval is verified (Phase 3 gate).
- Do **not** introduce an `ANTHROPIC_API_KEY` path or anything that moves auth off the subscription.

---

## Technical details
- `webapp.py`
  - `api_login` (~1088), `_check_rate_limit`/`_record_attempt` (~339–350): real-IP extraction + no-self-lockout.
  - Startup/`start()` and `bot.py` env load: empty-`WEB_PASSWORD` fail-fast.
  - `auth_middleware` (~390): unchanged for `/api/*`; TOTP gating lives in the login flow, not the middleware (cookie still represents an authenticated session).
  - Cookie set in `api_login` (~1106): keep `httponly`/`samesite=Lax`; ensure `secure`.
- Secret resolution call sites (per ARCHITECTURE "Secrets flow"): `bot.py:run_agent`, `webapp.py:api_project_chat`, `webapp.py:_run_card` — all currently call `_secrets_read(cwd)`. Phase 3 adds a resolver layer in front so a value of `vault:<item>` is dereferenced before injection into the agent env.
- Network: bind is `os.environ["WEB_HOST"]` (code default `127.0.0.1`, deployed `.env` uses `0.0.0.0`). Firewall the cockpit port instead of changing the bind.
- TOTP: standard RFC-6238; a small dependency (e.g. `pyotp`) is acceptable, pinned.

---

## Edge cases
- Behind the proxy every request may share one source IP → the rate-limiter must degrade to *delay*, not *deny-all*, so the operator is never collectively locked out.
- Lost authenticator → recovery codes; lost recovery codes → host-shell break-glass.
- Vault backend unreachable at run time → secret resolution must fail **loud** (the run errors with a clear message), never silently inject an empty/stale value.
- Vault reference that doesn't exist → explicit error, not blank.
- Restart aborts the current run — deploy each phase deliberately, not mid-task.

---

## How to verify
1. **Phase 0:** from two "different IPs" (spoofed `CF-Connecting-IP`), confirm one attacker's failures don't lock the other out; confirm 5 bad passwords from the operator's real IP slow down but recover; start the service with empty `WEB_PASSWORD` → it exits with a clear error; `ls -l .env` shows `0600`.
2. **Phase 1:** cockpit loads through the tunnel; direct LAN hit to `host:port` is refused; tunnel still works.
3. **Phase 2:** log in → prompted for TOTP; correct code → in; wrong code → denied; recovery code works once; break-glass documented and tested on a throwaway secret.
4. **Phase 3:** set a project secret to a `vault:<item>` reference → agent run sees the real value in its env; the value never appears in `secrets.env`, timeline, audit, or sidecars; remove the plaintext credentials file only after retrieval is verified.

---

## Deferred / later
- **Narrow HTTP-browser hardening (optional):** the cockpit's HTTP file browser can *serve* `.ssh` keys and `.claude/.credentials.json` to any authenticated session. The agent reads those via its own runtime, not this HTTP API, so blocklisting them in the HTTP browser would harden the "stolen session downloads portable keys" path without reducing Claude's capability. Cheap extra layer; deferred.
- **Per-project secrets adopting the same encryption:** today `.claude-ops/secrets/secrets.env` is plaintext 0600. It can later reference the global store (`secret:<name>`) or adopt the same Fernet engine. Out of scope for this pass.
