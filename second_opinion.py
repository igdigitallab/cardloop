"""Optional "second opinion" MCP tool — a Class-C provider bridge (see spec-060).

Registers a single in-process SDK MCP tool — `mcp__antigravity__second_opinion` — that
lets the main Claude agent consult a *different* model family mid-task: an independent
cross-model sanity check before a risky step, a long-context analysis, or offloading a
bulky read/summary onto a separate quota/credit pool instead of the Anthropic subscription.

Two backends, chosen per call by the `model` alias:
  * **Antigravity (`agy` CLI)** — Google AI Pro quota: flash / pro / opus / sonnet / gpt.
    Free at point of use but occasionally flaky (may return nothing).
  * **Azure AI Foundry (HTTP)** — the operator's Azure credits: grok / deepseek / gpt5.
    Reliable, higher quota; billed to sponsored credits. Configured via env
    (`AZURE_FOUNDRY_KEY`, `AZURE_FOUNDRY_ENDPOINT`); absent → those aliases don't appear.

A `panel` flag asks EVERY configured provider concurrently and returns all answers for the
main agent to synthesize (for high-stakes forks only).

Fully optional. If neither `agy` nor Azure is available (or SECOND_OPINION=0), no server is
built and the tool simply never appears — nothing else in the engine changes.

Design notes from probing the real backends:
  * `agy` REJECTS an unknown --model: exit 1, empty stdout, "invalid model selection" on
    stderr (older builds fell back silently; they no longer do). A hardcoded model name
    therefore turns into a hard outage the day Antigravity retires that model -- which is
    exactly what happened to "Gemini 3.5 Flash (High)". Hence the alias map is a *fallback*
    and the live `agy models` catalog is the source of truth (see _resolve_model).
  * stdout carries the clean answer; some environments interleave log noise, so we strip
    the known noise line shapes defensively.
  * an empty prompt makes `agy` print "Error: empty prompt" (exit 0) — guarded here.
  * agy runs via asyncio.create_subprocess_exec (NEVER blocking) so a multi-second call
    cannot freeze the engine's event loop; Azure runs via aiohttp with a total timeout.
  * Azure GPT-5.x reasoning models reject `max_tokens` and require `max_completion_tokens`
    — the caller self-heals by retrying with the alternate parameter on that exact error.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

# Alias -> exact agy model string. Keep this the single source of truth for what the
# Antigravity backend accepts; an alias outside every map is coerced to the default.
_MODEL_ALIASES = {
    "flash":  "Gemini 3.8 Flash (High)",
    "pro":    "Gemini 3.1 Pro (High)",
    "opus":   "Claude Opus 4.6 (Thinking)",
    "sonnet": "Claude Sonnet 4.6 (Thinking)",
    "gpt":    "GPT-OSS 120B (Medium)",
}
_DEFAULT_ALIAS = "pro"

# Alias -> slug pattern, matched against the LIVE `agy models` catalog (slug<TAB>display).
# The catalog is listed newest-first, so the first match is the current generation and the
# map above only has to survive until the next `agy models` call succeeds.
_ALIAS_SLUG_RE = {
    "flash":  re.compile(r"^gemini-[\d.]+-flash-high$"),
    "pro":    re.compile(r"^gemini-[\d.]+-pro-high$"),
    "opus":   re.compile(r"^claude-opus-[\w.-]+$"),
    "sonnet": re.compile(r"^claude-sonnet-[\w.-]+$"),
    "gpt":    re.compile(r"^gpt-oss-[\w.-]+$"),
}
# Measured 2026-09-21 with a canary marker at the END of the prompt: at ~160 000 chars
# agy still finds it, at 200 000 it does not -- and it answers confidently anyway, exit 0.
# A silently truncated read is indistinguishable from a good one and worse than no answer,
# so the call is refused above this instead of warned about. Override deliberately.
_AGY_MAX_INPUT_CHARS = int(os.getenv("SECOND_OPINION_AGY_MAX_CHARS", "160000"))
_CATALOG_TTL_SEC = 3600.0
_catalog_cache: tuple[float, list[tuple[str, str]]] | None = None
_catalog_lock: asyncio.Lock | None = None

# --- Azure AI Foundry backend (spec-060 Phase B: a second Class-C provider) ---
# Alias -> Foundry *deployment name*. These are the deployments on the operator's AI
# Foundry resource; override the whole map with AZURE_FOUNDRY_MODELS (JSON) if yours differ.
_AZURE_ALIASES_DEFAULT = {
    "grok":     "grok-4-3",      # xAI Grok 4.3
    "deepseek": "deepseek-v4",   # DeepSeek-V4-Pro
    "gpt5":     "gpt-5-1",       # OpenAI GPT-5.1
}
_AZURE_API_VERSION = "2024-05-01-preview"

# Defensive: clean answers go to stdout, but some envs (no ripgrep / first load) interleave
# log lines. Strip these shapes; keep everything else.
_NOISE_RE = re.compile(
    r"^(?:[IWE]\d{4} |Ripgrep is not available|Falling back to GrepTool|.*\bloaded in\b|.*\bdeprecat)"
)


def _resolve_agy() -> str | None:
    """Locate the agy binary.

    The systemd unit's PATH does NOT include ~/.local/bin, so `shutil.which("agy")`
    returns None under the service even though the binary exists. Hence the explicit
    home-dir fallback. Override the location with the AGY_BIN env var.
    """
    cand = os.getenv("AGY_BIN") or shutil.which("agy")
    if cand and Path(cand).is_file():
        return cand
    fallback = Path.home() / ".local" / "bin" / "agy"
    return str(fallback) if fallback.is_file() else None


def _enabled() -> bool:
    """Feature flag. Default on; the per-backend detection gates still apply separately."""
    return os.getenv("SECOND_OPINION", "1") not in ("0", "false", "False")


def _strip_noise(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _NOISE_RE.match(ln)).strip()


# --- Azure config helpers -----------------------------------------------------

def _azure_key() -> str | None:
    return (os.getenv("AZURE_FOUNDRY_KEY") or "").strip() or None


def _azure_endpoint() -> str | None:
    ep = (os.getenv("AZURE_FOUNDRY_ENDPOINT") or "").strip().rstrip("/")
    return ep or None


def _azure_models() -> dict:
    """Alias -> deployment map, overridable via AZURE_FOUNDRY_MODELS (JSON object)."""
    raw = os.getenv("AZURE_FOUNDRY_MODELS")
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict) and m:
                return {str(k).lower(): str(v) for k, v in m.items()}
        except Exception:
            pass
    return dict(_AZURE_ALIASES_DEFAULT)


def _azure_configured() -> bool:
    return bool(_azure_key() and _azure_endpoint())


async def _agy_catalog() -> list[tuple[str, str]]:
    """`agy models` as [(slug, display), ...], newest-first, cached for an hour.

    Never raises and never blocks the loop: on any failure it returns [] and the caller
    falls back to the static alias map.
    """
    global _catalog_cache, _catalog_lock
    if _catalog_lock is None:
        _catalog_lock = asyncio.Lock()
    async with _catalog_lock:
        now = asyncio.get_running_loop().time()
        if _catalog_cache and now - _catalog_cache[0] < _CATALOG_TTL_SEC:
            return _catalog_cache[1]
        agy = _resolve_agy()
        rows: list[tuple[str, str]] = []
        if agy:
            try:
                proc = await asyncio.create_subprocess_exec(
                    agy, "models",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                for line in (out_b or b"").decode("utf-8", "replace").splitlines():
                    if "\t" in line:
                        slug, _, display = line.partition("\t")
                        rows.append((slug.strip(), display.strip()))
            except Exception:
                rows = []
        if rows:
            _catalog_cache = (now, rows)
        return rows


async def _resolve_model(alias: str) -> str:
    """Alias -> a model string `agy` accepts TODAY.

    Prefers the live catalog (so a retired generation self-heals); falls back to the static
    map when `agy models` is unavailable. Returns the default alias's model for an unknown
    alias, matching the tool schema's own coercion.
    """
    static = _MODEL_ALIASES.get(alias, _MODEL_ALIASES[_DEFAULT_ALIAS])
    pattern = _ALIAS_SLUG_RE.get(alias)
    if pattern is None:
        return static
    catalog = await _agy_catalog()
    if not catalog:
        return static
    for slug, display in catalog:
        if pattern.match(slug):
            return display or slug
    # Alias family gone entirely (e.g. GPT-OSS pulled): static name keeps the error honest.
    return static


def _agy_stream_answer(raw: str) -> str:
    """Final answer out of agy's stream-json output.

    The answer lives in the last ``{"event":"result","result":{"response":...}}`` line.
    Anything that is not recognisable NDJSON falls back to the old plain-stdout read, so
    a format change degrades to "noisy but present" instead of "silently empty".
    """
    answer, saw_result = "", False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("event") != "result":
            continue
        saw_result = True
        res = msg.get("result") or {}
        if res.get("response"):
            answer = res["response"]
        elif res.get("error"):
            answer = f"agy error: {res['error']}"
    return answer.strip() if saw_result else _strip_noise(raw)


async def _ask_agy(question: str, alias: str, context: str | None) -> str:
    """Run one agy print-mode call and return a human-readable answer (or a clean
    "unavailable" string the agent can read and move on from — never raises)."""
    agy = _resolve_agy()
    if not agy:
        return "⚠️ second_opinion unavailable: the `agy` (Antigravity) binary was not found."

    model = await _resolve_model(alias)
    prompt = question if not context else f"{question}\n\n--- CONTEXT ---\n{context}"
    if len(prompt) > _AGY_MAX_INPUT_CHARS:
        return (f"⚠️ second_opinion refused: {len(prompt)} chars exceeds the {_AGY_MAX_INPUT_CHARS}-char "
                "ceiling above which Antigravity silently drops the tail of the input and answers "
                "from the beginning only. Trim `context`, or ask an Azure model "
                "(grok/deepseek/gpt5), which is not affected by this.")
    timeout = float(os.getenv("SECOND_OPINION_TIMEOUT", "180"))
    max_chars = int(os.getenv("SECOND_OPINION_MAX_CHARS", "6000"))

    # ⚠️ The prompt goes in over STDIN, never as an argv string. A single argument is
    # capped at MAX_ARG_STRLEN (131071 B) -- NOT the 2 MB ARG_MAX -- so passing it as
    # `-p <prompt>` made every call above ~128 KB die with E2BIG ("failed to launch agy")
    # *below* the 160 000-char ceiling this function advertises. agy's stdin channel is
    # `--input-format stream-json` plus one NDJSON line; note the envelope key is "event",
    # not the Claude-style "type" (that one is rejected outright).
    payload = json.dumps(
        {"event": "user",
         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        ensure_ascii=False,
    ) + "\n"
    try:
        proc = await asyncio.create_subprocess_exec(
            agy, "-p", "", "--model", model,
            "--input-format", "stream-json", "--output-format", "stream-json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # binary vanished / not executable / OS error
        return f"⚠️ second_opinion failed to launch agy: {e}"

    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return f"⚠️ second_opinion timed out after {int(timeout)}s (model: {model})."

    out = _agy_stream_answer((out_b or b"").decode("utf-8", "replace"))
    if proc.returncode and not out:
        err = _strip_noise((err_b or b"").decode("utf-8", "replace"))
        return f"⚠️ second_opinion error (exit {proc.returncode}): {err[:500] or 'no output'}"
    if not out or out.startswith("Error: empty prompt"):
        return "⚠️ second_opinion returned no usable answer."
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n…[truncated at {max_chars} chars]"
    return f"[second opinion · {model}]\n\n{out}"


async def _azure_chat_once(url: str, key: str, deployment: str, prompt: str,
                           max_out: int, token_param: str, timeout: float):
    """One HTTP POST to the Foundry inference endpoint. Returns (status, json|None, err)."""
    try:
        import aiohttp
    except Exception as e:
        return None, None, f"aiohttp unavailable: {e}"
    body = {
        "model": deployment,
        "messages": [{"role": "user", "content": prompt}],
        token_param: max_out,
    }
    headers = {"api-key": key, "Content-Type": "application/json"}
    try:
        cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=cfg) as sess:
            async with sess.post(url, headers=headers, json=body) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data, None
    except asyncio.TimeoutError:
        return None, None, "timeout"
    except Exception as e:
        return None, None, str(e)


async def _ask_azure(question: str, alias: str, context: str | None) -> str:
    """One Azure AI Foundry chat call. Returns a readable answer or a clean
    'unavailable/error' string — never raises. Billed to the operator's Azure credits."""
    key = _azure_key()
    endpoint = _azure_endpoint()
    if not key or not endpoint:
        return ("⚠️ second_opinion (Azure) unavailable: set AZURE_FOUNDRY_KEY and "
                "AZURE_FOUNDRY_ENDPOINT.")
    deployment = _azure_models().get(alias)
    if not deployment:
        return f"⚠️ second_opinion (Azure) has no deployment for alias {alias!r}."

    prompt = question if not context else f"{question}\n\n--- CONTEXT ---\n{context}"
    timeout = float(os.getenv("SECOND_OPINION_TIMEOUT", "180"))
    max_chars = int(os.getenv("SECOND_OPINION_MAX_CHARS", "6000"))
    max_out = int(os.getenv("SECOND_OPINION_AZURE_MAX_TOKENS", "2500"))
    url = f"{endpoint}/models/chat/completions?api-version={_AZURE_API_VERSION}"

    # GPT-5.x reasoning models reject max_tokens; self-heal to max_completion_tokens on
    # that exact error. Also do one retry on a transient network/timeout failure.
    token_param = "max_completion_tokens" if str(deployment).startswith("gpt-5") else "max_tokens"
    status, data, err = await _azure_chat_once(url, key, deployment, prompt, max_out, token_param, timeout)

    if status == 400 and isinstance(data, dict):
        msg = str(((data.get("error") or {}).get("message")) or "")
        if "max_completion_tokens" in msg and token_param != "max_completion_tokens":
            status, data, err = await _azure_chat_once(url, key, deployment, prompt, max_out,
                                                       "max_completion_tokens", timeout)
        elif "max_tokens" in msg and "not supported" in msg and token_param != "max_tokens":
            status, data, err = await _azure_chat_once(url, key, deployment, prompt, max_out,
                                                       "max_tokens", timeout)

    if err and status is None:  # transient failure → one retry
        status, data, err2 = await _azure_chat_once(url, key, deployment, prompt, max_out, token_param, timeout)
        err = err2 if status is None else None

    if status is None:
        return f"⚠️ second_opinion (Azure) failed ({err}) for model {deployment}."
    if status != 200 or not isinstance(data, dict):
        detail = ""
        if isinstance(data, dict):
            detail = str(((data.get("error") or {}).get("message")) or data)[:400]
        return f"⚠️ second_opinion (Azure) HTTP {status} for {deployment}: {detail}"

    try:
        out = (data["choices"][0]["message"]["content"] or "").strip()
        finish = data["choices"][0].get("finish_reason")
    except Exception:
        return f"⚠️ second_opinion (Azure) unexpected response shape from {deployment}."
    if not out:
        hint = " (hit the token cap before emitting an answer; raise SECOND_OPINION_AZURE_MAX_TOKENS)" \
               if finish == "length" else ""
        return f"⚠️ second_opinion (Azure) returned an empty answer from {deployment}{hint}."
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n…[truncated at {max_chars} chars]"
    return f"[second opinion · Azure/{deployment}]\n\n{out}"


async def _ask_panel(question: str, context: str | None) -> str:
    """Ask every configured provider concurrently; return all answers labeled, for the
    main agent to synthesize. Individual failures are reported inline, never fatal."""
    labels: list[str] = []
    tasks = []
    if _azure_configured():
        for alias in _azure_models():
            labels.append(f"Azure/{alias}")
            tasks.append(_ask_azure(question, alias, context))
    if _resolve_agy():
        labels.append("Antigravity/pro")
        tasks.append(_ask_agy(question, _DEFAULT_ALIAS, context))
    if not tasks:
        return "⚠️ second_opinion panel unavailable: no providers configured."
    results = await asyncio.gather(*tasks, return_exceptions=True)
    parts = []
    for label, res in zip(labels, results):
        body = res if isinstance(res, str) else f"⚠️ error: {res}"
        parts.append(f"===== {label} =====\n{body}")
    return ("[second opinion · PANEL — synthesize across these, flag disagreements]\n\n"
            + "\n\n".join(parts))


def _build_input_schema() -> dict:
    """Build the tool input schema with the enum of *currently available* model aliases."""
    aliases = list(_MODEL_ALIASES.keys())
    if _azure_configured():
        aliases += [a for a in _azure_models().keys() if a not in aliases]
    return {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question / task to send to the other model.",
            },
            "model": {
                "type": "string",
                "enum": aliases,
                "description": "Which model to consult. Azure credits (reliable): "
                               "grok=xAI Grok 4.3, deepseek=DeepSeek-V4, gpt5=OpenAI GPT-5.1. "
                               "Google quota (free, sometimes flaky): flash/pro=Gemini, "
                               "opus/sonnet=Claude-via-Google, gpt=GPT-OSS. Default: pro.",
            },
            "panel": {
                "type": "boolean",
                "description": "If true, ask a DIVERSE PANEL (every configured provider) "
                               "concurrently and return all answers for YOU to synthesize. "
                               "Use only for high-stakes forks; costs more, ignores 'model'.",
            },
            "context": {
                "type": "string",
                "description": "Optional extra context (code, log, doc excerpt) to attach.",
            },
        },
        "required": ["question"],
    }


async def _second_opinion_handler(args: dict) -> dict:
    """MCP tool body, kept at module level (not a closure) so it is unit-testable.
    Validates input, routes to the right backend by alias, or runs a panel.
    Always returns an MCP text-content result; never raises."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"content": [{"type": "text",
                             "text": "⚠️ second_opinion needs a non-empty 'question'."}]}
    context = (args.get("context") or "").strip() or None

    if args.get("panel"):
        text = await _ask_panel(question, context)
        return {"content": [{"type": "text", "text": text}]}

    alias = (args.get("model") or _DEFAULT_ALIAS).strip().lower()
    if alias in _azure_models() and _azure_configured():
        text = await _ask_azure(question, alias, context)
    elif alias in _MODEL_ALIASES:
        text = await _ask_agy(question, alias, context)
    else:  # unknown alias (or Azure alias with Azure off) → default Google model
        alias = _DEFAULT_ALIAS
        text = await _ask_agy(question, alias, context)
    return {"content": [{"type": "text", "text": text}]}


def build_antigravity_server() -> dict | None:
    """Build the SDK MCP server exposing `second_opinion`, ready to drop into
    ClaudeAgentOptions(mcp_servers=...).

    Returns a ``{"antigravity": <server_config>}`` dict, or ``None`` when the feature is
    disabled or NO backend is available (so the caller can pass ``mcp_servers=result or {}``).
    The server key stays "antigravity" for backward-compat even though it now also fronts
    Azure. Call it once at engine import — building the server invokes nothing; each tool
    call reaches out fresh, so there is nothing to go stale.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except Exception:
        return None
    if not _enabled() or (not _resolve_agy() and not _azure_configured()):
        return None

    @tool(
        "second_opinion",
        "Consult a DIFFERENT model family for an independent second opinion, billed to a "
        "separate pool (Azure credits or Google quota) — not the Anthropic budget. Use "
        "SPARINGLY, only when it genuinely helps: a cross-model sanity check before a risky "
        "or irreversible step, an ambiguous architecture fork, when stuck after ~2 attempts, "
        "or when the operator asks. NOT for routine steps. Pick a `model` (grok/deepseek/gpt5 "
        "= Azure, reliable; pro/flash/opus/sonnet/gpt = Google, free but flaky) or set "
        "`panel`=true to poll every provider at once. Returns the other model's answer(s) as text.",
        _build_input_schema(),
    )
    async def second_opinion(args: dict) -> dict:
        return await _second_opinion_handler(args)

    server = create_sdk_mcp_server(name="antigravity", version="1.1.0", tools=[second_opinion])
    return {"antigravity": server}


# The stable tool name the agent sees: mcp__<server-key>__<tool-name>.
TOOL_NAME = "mcp__antigravity__second_opinion"
