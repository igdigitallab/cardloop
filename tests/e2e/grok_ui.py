"""Shared UI/API helpers of the Grok e2e files (spec-095 P5b)."""
import json




def api(page, server, path, method="GET", body=None):
    url = server["base_url"] + path
    if method == "GET":
        r = page.request.get(url)
    else:
        r = page.request.fetch(url, method=method, data=json.dumps(body or {}),
                               headers={"Content-Type": "application/json"})
    try:
        payload = r.json()
    except Exception:
        payload = r.text()
    return r.status, payload


def chats_of(page, server, project_id):
    status, body = api(page, server, f"/api/projects/{project_id}/chats")
    assert status == 200, body
    return body["chats"]


def open_new_chat_dialog(page):
    page.click(".chat-named-tab-new")
    page.wait_for_selector("text=New agent chat")


def create_chat(page, provider: str, name: str) -> None:
    """Drives the real New-chat dialog; returns once the new chat's tab is the active one."""
    open_new_chat_dialog(page)
    page.click(f"button[data-provider={provider}]")
    page.fill("input[placeholder^='e.g. Math']", name)
    page.click("button:has-text('Create chat')")
    page.wait_for_selector(f".chat-named-tab.active:has-text('{name}')")


def watch_text(page, needle: str) -> None:
    """Records, from now on, whether `needle` was EVER on screen (see the module docstring)."""
    page.evaluate("""needle => {
        window.__seen = window.__seen || {}
        const check = () => { if (document.body.innerText.includes(needle)) window.__seen[needle] = true }
        new MutationObserver(check).observe(document.body, {subtree: true, childList: true, characterData: true})
        check()
    }""", needle)


def wait_seen(page, needle: str, timeout: int = 20_000) -> None:
    page.wait_for_function("n => window.__seen && window.__seen[n]", arg=needle, timeout=timeout)


def usage_rows(server) -> list[dict]:
    path = server["app_dir"] / "data" / "grok_usage.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def claude_transcripts(server, project_id: str) -> list:
    """Transcripts the (fake) Claude engine wrote for this project — proof it ran."""
    root = server["home"] / ".claude" / "projects"
    return [p for p in root.glob(f"*{project_id}*/*.jsonl")] if root.exists() else []


def open_tab(page, label: str) -> None:
    page.click(f".tab-btn:has-text('{label}')")


def open_model_menu(page) -> None:
    page.click(".composer-modelthink-btn")
    page.wait_for_selector(".composer-modelthink-menu")


