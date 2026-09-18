#!/home/igor/cardloop/venv/bin/python
"""Paste a whole text into the focused field of the cockpit browser pane.

`browser_type` sends real keystrokes, so every newline is an Enter — in chat widgets
(Fiverr inbox, etc.) that sends the message in pieces. This goes through the pane's
operator-input channel as {t:'paste'} → CDP Input.insertText: one insertion, newlines
kept, no key events. Focus the field first (browser_click on it).

Usage:  pane-paste.py <project-id> <file>      (use - for stdin)
Needs:  `secret get claude-ops-web-password`; cockpit listening on 127.0.0.1:8787.
"""
import asyncio
import json
import subprocess
import sys

import aiohttp

BASE = "http://127.0.0.1:8787"


async def main(project: str, text: str) -> None:
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
            await ws.send_str(json.dumps({"t": "paste", "text": text}))
            await asyncio.sleep(1)
    print(f"pasted {len(text)} chars into pane of project {project}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    body = sys.stdin.read() if sys.argv[2] == "-" else open(sys.argv[2], encoding="utf-8").read()
    asyncio.run(main(sys.argv[1], body.rstrip("\n")))
