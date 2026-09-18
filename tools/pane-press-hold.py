#!/home/igor/cardloop/venv/bin/python
"""Press-and-hold inside the cockpit browser pane (PerimeterX «PRESS & HOLD» and similar).

`browser_solve_captcha` only covers reCAPTCHA / hCaptcha / Turnstile, and `browser_click`
releases the button immediately. This sends a real held press through the pane's own
operator-input channel (/api/browser/input-ws): move → down → small moves while held → up.

Usage:  pane-press-hold.py <project-id> <x> <y> [hold_seconds=12]
        (x, y — viewport coordinates of the button, read them off browser_screenshot)
Needs:  `secret get claude-ops-web-password`; cockpit listening on 127.0.0.1:8787.
"""
import asyncio
import json
import subprocess
import sys

import aiohttp

BASE = "http://127.0.0.1:8787"


async def main(project: str, x: float, y: float, hold: float) -> None:
    pw = subprocess.run(["secret", "get", "claude-ops-web-password"],
                        capture_output=True, text=True, check=True).stdout.strip()
    async with aiohttp.ClientSession() as s:
        r = await s.post(BASE + "/api/login", json={"password": pw})
        cookie = r.cookies.get("cops_auth")
        if r.status != 200 or cookie is None:
            sys.exit(f"login failed: HTTP {r.status}")
        # The login cookie is not replayed by aiohttp's jar on a bare IP, so pass it by hand.
        headers = {"Cookie": "cops_auth=" + cookie.value}
        async with s.ws_connect(f"{BASE}/api/browser/input-ws?project={project}",
                                headers=headers) as ws:
            async def mouse(action: str, mx: float, my: float, buttons: int, **extra) -> None:
                await ws.send_str(json.dumps({"t": "mouse", "action": action, "x": mx, "y": my,
                                              "buttons": buttons, **extra}))

            for dx in (-60, -30, 0):  # approach the button like a hand would
                await mouse("move", x + dx, y + 5, 0)
                await asyncio.sleep(0.15)
            await mouse("down", x, y, 1, button="left", clickCount=1)
            held = 0.0
            while held < hold:
                await asyncio.sleep(0.5)
                held += 0.5
                await mouse("move", x + (held % 2), y, 1)
            await mouse("up", x, y, 0, button="left", clickCount=1)
            await asyncio.sleep(1)
    print(f"held {hold:.0f}s at ({x:.0f},{y:.0f}) in pane of project {project}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
                     float(sys.argv[4]) if len(sys.argv) > 4 else 12.0))
