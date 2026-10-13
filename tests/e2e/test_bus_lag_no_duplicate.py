"""
A reply must never show twice when the activity bus lags behind the chat POST stream.

Every content frame of a turn reaches the browser twice: on the activity bus (the render
source) and on the POST /chat response. Nothing orders the two sockets. When the POST frames
won the race (always on the first turn of a freshly started cockpit, measured 2026-10-13: 3 of
4 runs), its "canonical final text" repair wrote the whole reply into the open bubble without
recording the frame's seq, the end of the POST closed that bubble, and the bus's own deltas
then opened a second bubble with the same text. It stayed on screen until the completion
reconcile replaced the canvas from history about 1.2 s later.

The race is made deterministic here: an init script delays every chunk of the activity stream
(order preserved), so the POST stream always runs ahead. A MutationObserver records the most
copies of each text ever on screen, and whether one vanished, not just the settled state.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

_LAG_THE_BUS = """
(lagMs => {
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const res = await origFetch(input, init);
    if (!url.includes('/activity-stream') || !res.body) return res;
    const reader = res.body.getReader();
    const later = fn => setTimeout(fn, lagMs);
    const body = new ReadableStream({
      start(controller) {
        (async () => {
          try {
            for (;;) {
              const { value, done } = await reader.read();
              if (done) { later(() => controller.close()); return; }
              later(() => controller.enqueue(value));
            }
          } catch (e) { later(() => controller.error(e)); }
        })();
      },
      cancel(reason) { return reader.cancel(reason); },
    });
    return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
  };
})(%d);
"""

# Per marker: the most copies ever on screen at once, and whether it vanished after showing.
_WATCH = """
([selector, markers]) => {
  window.__seen = Object.fromEntries(markers.map(m => [m, { max: 0, vanished: false }]));
  const tick = () => {
    const els = [...document.querySelectorAll(selector)];
    for (const m of markers) {
      const n = els.filter(e => (e.textContent || '').includes(m)).length;
      const s = window.__seen[m];
      if (s.max > 0 && n === 0) s.vanished = true;
      s.max = Math.max(s.max, n);
    }
  };
  new MutationObserver(tick).observe(document.body, { subtree: true, childList: true, characterData: true });
}
"""


def _lagged_chat(page, project_id: str, lag_ms: int) -> None:
    """Delays every chunk of the activity stream by lag_ms (order kept), then opens the chat."""
    page.add_init_script(_LAG_THE_BUS % lag_ms)
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, project_id)


def _watch(page, selector: str, markers: list[str]) -> None:
    page.evaluate(_WATCH, [selector, markers])


def test_a_reply_shows_once_while_the_bus_lags_behind_the_post_stream(logged_in_page):
    page = logged_in_page
    lag_ms = 1000  # well under the 8 s after which the client treats the bus as dead
    _lagged_chat(page, "e2e-buslag", lag_ms)
    reply = "a scripted e2e reply."
    _watch(page, ".chat-msg-assistant .chat-msg-body", [reply])
    page.evaluate("() => { window.__seenPrompt = 0; new MutationObserver(() => {"
                  " const n = [...document.querySelectorAll('.chat-msg-user')]"
                  ".filter(e => (e.textContent || '').includes('e2e:text')).length;"
                  " window.__seenPrompt = Math.max(window.__seenPrompt, n) })"
                  ".observe(document.body, { subtree: true, childList: true, characterData: true }) }")
    send_chat(page, "e2e:text")

    # The settled reply, then past the completion reconcile (1.2 s after run_end) and the lag.
    page.wait_for_function(
        "r => [...document.querySelectorAll('.chat-msg-assistant .chat-msg-body')]"
        ".some(e => (e.textContent || '').includes(r))", arg=reply, timeout=30_000)
    page.wait_for_timeout(lag_ms + 4000)

    seen = page.evaluate("window.__seen")[reply]
    assert seen["max"] == 1, f"the reply was on screen {seen['max']} times at once"
    assert page.evaluate("window.__seenPrompt") == 1, "the prompt was on screen twice"


def test_text_blocks_split_by_tools_stay_put_while_the_bus_lags(logged_in_page):
    # The POST stream's canonical-final repair may touch the open bubble only after the bus has
    # drawn the tool that started the block: with the bus 2.5 s behind, block two's final text
    # reaches the POST stream while the open bubble is still block one's.
    page = logged_in_page
    lag_ms = 2500
    _lagged_chat(page, "e2e-buslag-multi", lag_ms)
    blocks = ["BLOCK_ONE_alpha done.", "BLOCK_TWO_beta done.", "BLOCK_THREE_gamma done."]
    _watch(page, ".chat-msg-assistant .chat-msg-body", blocks)
    send_chat(page, "e2e:multiblock")

    page.wait_for_function(
        "b => b.every(t => [...document.querySelectorAll('.chat-msg-assistant .chat-msg-body')]"
        ".some(e => (e.textContent || '').includes(t)))", arg=blocks, timeout=30_000)
    page.wait_for_timeout(lag_ms + 6000)  # the scenario's silent tail, the lag, the reconcile

    seen = page.evaluate("window.__seen")
    for b in blocks:
        assert seen[b]["max"] == 1, f"{b} was on screen {seen[b]['max']} times at once"
        assert not seen[b]["vanished"], f"{b} disappeared after it was shown"
