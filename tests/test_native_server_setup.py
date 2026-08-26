"""Native (Capacitor) server picker — guards on the parts a typo silently breaks.

There is no JS test runner in this repo, so these assert the source-level contracts
that TypeScript cannot: that both translations carry the same keys, that every key
the native screens reference actually exists, and that the passphrase handed to the
server never gets written to disk on the device.
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
I18N = HERE / "web" / "src" / "i18n"
NATIVE = HERE / "web" / "src" / "native"

_KEY_RE = re.compile(r"^\s*'([a-z0-9_.]+)':", re.MULTILINE)


def _keys(name: str) -> set:
    return set(_KEY_RE.findall((I18N / name).read_text(encoding="utf-8")))


def test_native_namespace_is_translated_in_both_files():
    """Scoped to `native.*` on purpose: the wider en/ru sets are NOT in sync today
    (22 English keys have no Russian counterpart), and ru.ts is not wired into the
    runtime at all — `i18n/index.ts` exports `t = en` unconditionally. Asserting full
    parity here would fail on a pre-existing gap this feature did not create; asserting
    it for the namespace this feature owns keeps the new strings honest."""
    en = {k for k in _keys("en.ts") if k.startswith("native.")}
    ru = {k for k in _keys("ru.ts") if k.startswith("native.")}
    assert en and en == ru, {"only_en": sorted(en - ru), "only_ru": sorted(ru - en)}


def test_every_native_i18n_key_used_in_the_ui_exists():
    used = set()
    for f in NATIVE.glob("*.tsx"):
        used |= set(re.findall(r"t\['(native\.[a-z0-9_.]+)'\]", f.read_text(encoding="utf-8")))
    login = (HERE / "web" / "src" / "components" / "LoginScreen.tsx").read_text(encoding="utf-8")
    used |= set(re.findall(r"t\['(native\.[a-z0-9_.]+)'\]", login))
    assert used, "expected the native screens to reference i18n keys"
    assert used <= _keys("en.ts")


def test_the_change_server_control_is_translated():
    """The escape hatch out of a dead server must not be the one untranslated string."""
    assert "native.change_server" in _keys("en.ts")
    assert "native.change_server" in _keys("ru.ts")


def test_passphrase_is_never_persisted_on_the_device():
    """The server cookie is what keeps the app signed in, so storing the passphrase
    would buy nothing and lose it to whoever picks up the phone. Only the server URL
    may be written."""
    boot = (NATIVE / "boot.tsx").read_text(encoding="utf-8")
    handoff = (NATIVE / "handoff.ts").read_text(encoding="utf-8")
    stored = set(re.findall(r"localStorage\.setItem\(\s*([A-Za-z_]+)", boot + handoff))
    assert stored <= {"STORAGE_KEY"}, stored
    for src in (boot, handoff):
        assert "setItem('cops" not in src, "a raw key here bypasses the allow-list above"


def test_handoff_uses_the_url_fragment_not_a_query_string():
    """A query string reaches the server and lands in its access log (and Cloudflare's);
    a fragment never leaves the client."""
    boot = (NATIVE / "boot.tsx").read_text(encoding="utf-8")
    assert "#${AUTH_HANDOFF_PARAM}=" in boot
    assert "?${AUTH_HANDOFF_PARAM}=" not in boot


def test_handoff_erases_the_token_before_using_it():
    """If the login request hangs or the app is killed mid-flight, the passphrase must
    not be left sitting in the address bar."""
    src = (NATIVE / "handoff.ts").read_text(encoding="utf-8")
    erase = src.index("history.replaceState")
    login = src.index("'/api/login'")
    assert erase < login, "the address is cleaned only AFTER the login call"


def test_saved_server_is_health_checked_before_redirecting():
    """Without the probe a dead saved server sends the WebView into a void with no UI
    left to fix it — the state this whole screen exists to escape."""
    gate = (NATIVE / "NativeGate.tsx").read_text(encoding="utf-8")
    assert "api/health" in gate
    assert "AbortController" in gate, "an unbounded probe hangs the app on a black hole"


def test_the_picker_only_ever_renders_on_the_app_bundle_origin():
    """localStorage is PER-ORIGIN, so on the server's origin the saved URL is not
    readable. Deciding "configured?" from that value there showed the picker a second
    time right after the first succeeded — and its submit then called replace() with a
    fragment-only URL change, which does not navigate, hanging on "Connecting...".
    The guard must key off the bundle host, never off the saved value."""
    boot = (NATIVE / "boot.tsx").read_text(encoding="utf-8")
    guard = "if (window.location.hostname !== APP_BUNDLE_HOST) return true"
    assert guard in boot
    # ...and it must come before anything reads the saved server.
    assert boot.index(guard) < boot.index("localStorage.getItem(STORAGE_KEY)")


def test_change_server_flag_travels_in_the_url_not_storage():
    """Same per-origin trap in the other direction: a flag written on the server's
    origin would be invisible to the bundle that has to act on it."""
    handoff = (NATIVE / "handoff.ts").read_text(encoding="utf-8")
    assert f"?${{SETUP_QUERY_PARAM}}=1" in handoff
    assert "localStorage.setItem" not in handoff


def test_bundle_host_matches_capacitor_config():
    """APP_BUNDLE_HOST is only correct while Capacitor keeps its default hostname."""
    cfg = (HERE / "web" / "capacitor.config.ts").read_text(encoding="utf-8")
    assert "hostname" not in cfg, "a custom server.hostname makes APP_BUNDLE_HOST wrong"
    assert "'localhost'" in (NATIVE / "keys.ts").read_text(encoding="utf-8")


def test_the_setup_probe_is_bounded():
    """An address that connects and never answers must not hang the button forever."""
    src = (NATIVE / "ServerSetup.tsx").read_text(encoding="utf-8")
    assert "AbortController" in src and "PROBE_TIMEOUT_MS" in src
