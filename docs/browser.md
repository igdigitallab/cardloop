# The live browser pane

Cardloop embeds a **real browser inside the cockpit** that the agent drives and you watch — live,
over a CDP screencast. You can also **drive it yourself, alongside the agent** (co-control): click,
scroll, and type — including logins — on both desktop and mobile. When you ask the agent to "open",
"launch", or "use the browser", it drives *this* pane, not some invisible headless process.

It's an **opt-in module** (off by default). Turn it on in **Settings → Extensions → Browser**; a
**🌐 Browser** tab then appears in each project.

---

## Three backends (all optional, pick per taste)

The browser backend is pluggable — one swap point, three tiers. The built-in default works out of the
box; the other two are opt-in and you can switch any time in **Extensions → Browser**.

| Backend | What it is | Setup | Cost |
|---|---|---|---|
| **Built-in Chromium** (default) | Vanilla Chromium via Playwright — no stealth | one Playwright install (below) | free |
| **CloakBrowser** | Anti-detect stealth Chromium (beats most bot detection) | one-click install in the UI | **free tier** (MIT) |
| **External CDP / Cloak Manager** | Connect to *any* CDP browser, or to persistent **logged-in** profiles | bring your own endpoint / Manager | your own infra |

### A. Built-in Chromium — the default

Zero config beyond a one-time Playwright install. Nothing stealthy, but perfect for general browsing
and scraping where you don't need to defeat bot detection.

```bash
venv/bin/pip install playwright && venv/bin/playwright install chromium
```

Then enable the Browser module and leave the backend on **Built-in Chromium**.

### B. CloakBrowser — free stealth, one click

[`cloakbrowser`](https://pypi.org/project/cloakbrowser/) is a public, **MIT-licensed** package with a
**free tier**: a patched Chromium (downloaded from GitHub Releases, no key, no limit) that defeats most
anti-bot fingerprinting (Cloudflare Turnstile, FingerprintJS, reCAPTCHA v3). `navigator.webdriver` reads
`false`, the UA looks like a real desktop browser, etc.

In **Extensions → Browser**, pick **CloakBrowser (stealth)** → click **Install CloakBrowser (free)**.
That runs, detached:

```bash
venv/bin/pip install cloakbrowser && venv/bin/python -m cloakbrowser install
```

A Pro tier (latest Chromium, subscription) exists too, but the free tier is all you need.

### C. External CDP / Cloak Manager — persistent logged-in profiles

Point the pane at **any** browser speaking the Chrome DevTools Protocol. Two ways:

- **Static CDP URL** — any Chrome started with `--remote-debugging-port`, a Browserless/Steel endpoint,
  etc. Just paste the URL.
- **Cloak Manager** — a service that manages **persistent profiles**: each profile is an isolated
  fingerprint with its own cookies/localStorage that survive restarts. You log into a site **once**
  (handling the captcha/2FA yourself in the Manager's built-in noVNC viewer), and from then on the agent
  reuses that **real, authenticated session** over CDP. Set the Manager URL + token in
  **Extensions → Browser → Cloak Manager**, click **Load profiles**, and **Use** the one you want.

> Cardloop ships only the **client**. There is no bundled Manager and no hardcoded URL or token — you run
> your own Manager (or use any CDP browser) and enter your endpoint in the UI. Your credentials go to the
> encrypted secret vault, never to `modules.json` or git.

**REST vs CDP host split.** If your Manager's REST API sits behind a CDN/WAF (fine for JSON) but raw CDP
websockets need a directly-reachable address, set `CLOAK_MANAGER_CDP_BASE` in `.env` to the internal
host (e.g. `http://10.0.0.5:8080`). REST keeps using the Manager URL; CDP uses this base. Unset → both
use the Manager URL.

---

## Co-control: you and the agent, together

The pane is **not** a one-way screencast. The same browser session is driven by both the agent (via its
tools) and you (mouse/keyboard in the pane):

- **Desktop** — click to focus the pane, then click/scroll/type normally.
- **Mobile** — tap = click, swipe = scroll, and tapping a field raises the on-screen keyboard so you can
  type logins/passwords from your phone.

This is the intended workflow for logged-in profiles: the agent navigates, and **you** handle the
sensitive bits (passwords, captcha, 2FA) right there in the same live session.

---

## Agent-action safety gate

Because a logged-in profile means **the agent acts as your identity**, mutating actions are gated.
In **Extensions → Browser → Agent actions**:

- **Read only** (default) — the agent may `navigate` and read the page (snapshot), nothing more.
- **Full** — the agent may also **click and type** (submit forms, post, etc.).

Read tools are always allowed; click/type are refused with a note until you flip the gate to **Full**.
Keep it on **Read only** for logged-in profiles unless you explicitly want the agent acting on your behalf.

The agent reaches the pane through MCP tools `browser_navigate`, `browser_snapshot`, `browser_click`, and
`browser_type` — exposed only while the Browser module is enabled.

---

## TL;DR

- A fresh install gets the **built-in Chromium** (Playwright) — safe, no stealth, one install command.
- Want stealth? **One click** installs the free CloakBrowser tier.
- Want the agent to work inside your **already-logged-in** sessions? Run your own Cloak Manager (or any
  CDP browser) and point the cockpit at it — nothing is shared or hardcoded.
- The agent is **read-only by default**; you decide when it may click and type.
