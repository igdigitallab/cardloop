# Notifications

Cardloop tells you when a run finishes — even when you're not looking at the tab, and even when the
app is fully closed. Click the notification to jump straight to the project.

There are three layers, and they work together so you never get a duplicate alert:

| Layer | Fires when | Needs |
|---|---|---|
| **1. In-app notification** | The app is open (any tab) but you're not looking at that project, or the window is minimized | Just the toggle below |
| **2. Installed app (PWA)** | — | "Install app" (see below) |
| **3. Web Push** | The app is **fully closed** / not running | Toggle + installed PWA + HTTPS |

When the app is open, layer 1 handles it. When it's closed, layer 3 does. The service worker suppresses
a push if any window is open, so you only ever get one notification.

The in-app notification (layer 1) is **gated**: it does *not* fire for the project tab you're currently
looking at — only for background projects. (Gating is per-project: all chats in a project share one
activity stream.)

## Enable it (once)

1. Open **Global Settings → 🔔 Notifications** and turn on the toggle.
2. Your browser will ask for permission — allow it.
   This enables both the in-app notification (layer 1) **and** subscribes you to Web Push (layer 3).

That's it. Run a task in one project, switch to another project or minimize — you'll get
`✅ <project>` (or `❌` on error). Click it to focus the window and open that project.

## Install as an app (for push when closed)

Web Push (layer 3) — and push on iOS at all — requires the cockpit to be **installed** as a PWA:

- **Android (Chrome/Edge):** browser menu → **Install app** / **Add to Home screen**.
- **Desktop (Chrome/Edge):** the install icon in the address bar.
- **iPhone (Safari):** **Share → Add to Home Screen**. On iOS, push works **only** for the installed
  PWA (iOS 16.4+).

After installing, fully close the app and run a task — the push should arrive like a messenger notification.

## Requirements & notes

- **HTTPS is required** for Web Push and service workers (localhost is exempt for development). If you
  reach the cockpit over a plain-HTTP IP, only layer 1 (in-app) works. Put it behind HTTPS — e.g. a
  Cloudflare Tunnel or a reverse proxy with TLS (see the main README, "Access from anywhere").
- The server auto-generates a **VAPID** keypair on first start (stored in `data/`, never committed).
  Optionally set `VAPID_SUBJECT=mailto:you@yourdomain.com` in `.env` to identify your instance to push
  gateways.
- Notifications are **opt-in** and off by default — no surprise permission prompts.

## Troubleshooting

- **No notification at all:** check the toggle is on and the browser shows permission *granted* (if it
  says *blocked*, re-allow notifications for the site in browser settings).
- **In-app works, push (closed app) doesn't:** make sure you're on **HTTPS** and the app is **installed**
  as a PWA. On iPhone, push requires the installed PWA.
- **Notification fires for the project I'm looking at:** it shouldn't — it's gated to background projects.
  If it does, the tab may not be registering as "active/visible" (e.g. a detached window).
