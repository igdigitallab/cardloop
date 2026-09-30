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
- Path traversal in the file/project APIs.
- Secret-vault disclosure beyond an authenticated session.
- Command injection via configurable commands (e.g. `log_cmd`).

Explicitly **out of scope** (these are by-design, documented behaviours, not bugs):
- An authenticated operator can run arbitrary work and read the decrypted vault — that is the
  product. The trust boundary is "authenticated operator," not "sandboxed agent."
- Exposing the cockpit without HTTPS / behind no auth — that's a deployment mistake; set
  `WEB_COOKIE_SECURE=true` and put it behind a reverse proxy.
- Reports from automated scanners that show no demonstrable impact on a default install.

## Supported versions

This is a young project; security fixes land on `master` and, from there, in the next tagged
release. Run the latest commit or the latest release tag.

## Our own hygiene

The repository is checked continuously: [CodeQL](.github/workflows/codeql.yml) static analysis,
[OpenSSF Scorecard](.github/workflows/scorecard.yml), Dependabot version and security updates
([dependabot.yml](.github/dependabot.yml)), GitHub secret scanning, and the automated test
suite in CI. Workflow actions are pinned to commit SHAs and run with a read-only token.
