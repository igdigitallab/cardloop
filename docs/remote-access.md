# Remote access — which option to use when

Cardloop binds `127.0.0.1` by default, so reaching it from your phone or away from home needs one of
three mechanisms. They are not alternatives to pick once — most people start with `--tunnel` to try
Cardloop out, then move to Tailscale or a named Cloudflare Tunnel once they know they'll keep using it.

| | `--tunnel` (quick tunnel) | Tailscale | Cloudflare Tunnel (named) |
|---|---|---|---|
| Setup | `bot.py --tunnel`, nothing else | install on host + phone | `cloudflared tunnel login` + DNS record |
| Account needed | No | Yes (free tier works) | Yes (free tier works) |
| Hostname | Random, **changes every restart** | Your Tailscale device name/IP | A subdomain you own, stable forever |
| Exposure | Public internet (password-protected) | Private — only your tailnet | Public internet (password-protected) |
| Good for | Trying Cardloop out, a one-off remote session, showing someone a demo | Personal daily use, nothing public-facing | A cockpit you bookmark / share a stable URL for |

## `--tunnel` — the on-ramp

`venv/bin/python bot.py --tunnel` (or `CARDLOOP_TUNNEL=1` in `.env` for the systemd service) spawns a
[`cloudflared`](https://github.com/cloudflare/cloudflared) **quick tunnel**
(`cloudflared tunnel --url http://127.0.0.1:8787 --no-autoupdate`) — no Cloudflare account, no config
file, no DNS record. Cloudflare hands back a random `https://<random-words>.trycloudflare.com` hostname,
which Cardloop parses off `cloudflared`'s own output and prints together with a scannable QR code, so you
can open the cockpit on your phone in one command.

Tradeoffs that make this an on-ramp, not a destination:

- **The hostname is ephemeral.** It changes every time the tunnel (re)starts — nothing to bookmark, no
  DNS record to reuse, and it will be different the next time you run `--tunnel`.
- **It's still the public internet.** Anyone with the URL can hit your cockpit's login page. The only
  protection is `WEB_PASSWORD` (+ TOTP if you've enabled it) — see [Security model](../README.md#security-model).
  Keep the password strong before you turn this on.
- **cloudflared must be installed.** If it isn't on `PATH`, Cardloop prints an install hint and keeps the
  cockpit running on localhost — `--tunnel` never blocks startup.
- **If `cloudflared` exits unexpectedly**, Cardloop restarts it with bounded backoff and logs the new URL
  (which will be different — see above). On shutdown (Ctrl-C or systemd stop), the `cloudflared` process
  is always terminated — nothing is left orphaned.
- Also works without `--tunnel`: if `WEB_HOST` is set to something other than `127.0.0.1` (LAN or
  `0.0.0.0`), Cardloop prints a QR code for that LAN URL too, so scanning your way in from a phone on the
  same network doesn't require typing an IP by hand either.

## What you keep — Tailscale or a named Cloudflare Tunnel

Both give you a **stable** address instead of a random one, at the cost of a one-time setup:

- **[Tailscale](https://tailscale.com)** puts the cockpit on your own private mesh network — nothing is
  exposed to the public internet at all, only devices you've enrolled can reach it. Simplest choice if
  it's just you.
- **[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  (named tunnel)** gives you a real subdomain on your own domain (`cockpit.example.com`) that keeps
  working across restarts, with free HTTPS via Cloudflare.

Setup steps for both live in the main [README → Access from anywhere](../README.md#access-from-anywhere-your-own-domain).
This doc only covers the tradeoff; it does not replace those instructions.
