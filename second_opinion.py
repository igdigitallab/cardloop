"""Optional "second opinion" MCP tool backed by Google Antigravity (`agy`).

Registers a single in-process SDK MCP tool — `mcp__antigravity__second_opinion` —
that lets the main Claude agent consult a *different* model family (Gemini / GPT-OSS /
Claude-via-Google) mid-task: an independent cross-model sanity check before a risky
step, a long-context analysis (Gemini ~1M), or offloading a bulky read/summary onto
the Google AI Pro quota instead of the Anthropic subscription.

Fully optional and the first concrete provider beyond Claude (see spec-060). If the
`agy` binary is absent or SECOND_OPINION=0, no server is built and the tool simply
never appears — nothing else in the engine changes.

Design notes from probing the real binary:
  * `agy` SILENTLY falls back to its default model on an unknown --model (exit 0, no
    error). We therefore ONLY ever pass an exact, validated model string from the map
    below — never a raw caller string.
  * stdout carries the clean answer; in some environments `agy` interleaves log noise,
    so we strip the known noise line shapes defensively.
  * an empty prompt prints "Error: empty prompt" to stdout with exit 0 — guarded here.
  * the call is run via asyncio.create_subprocess_exec (NEVER blocking subprocess) so a
    multi-second agy call cannot freeze the engine's event loop.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

# Alias -> exact agy model string. Keep this the single source of truth for what the
# tool will accept; an alias outside this map is coerced to the default before the call.
_MODEL_ALIASES = {
    "flash":  "Gemini 3.5 Flash (High)",
    "pro":    "Gemini 3.1 Pro (High)",
    "opus":   "Claude Opus 4.6 (Thinking)",
    "sonnet": "Claude Sonnet 4.6 (Thinking)",
    "gpt":    "GPT-OSS 120B (Medium)",
}
_DEFAULT_ALIAS = "pro"

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
    """Feature flag. Default on; the agy-detection gate still applies separately."""
    return os.getenv("SECOND_OPINION", "1") not in ("0", "false", "False")


def _strip_noise(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _NOISE_RE.match(ln)).strip()


async def _ask_agy(question: str, alias: str, context: str | None) -> str:
    """Run one agy print-mode call and return a human-readable answer (or a clean
    "unavailable" string the agent can read and move on from — never raises)."""
    agy = _resolve_agy()
    if not agy:
        return "⚠️ second_opinion unavailable: the `agy` (Antigravity) binary was not found."

    model = _MODEL_ALIASES.get(alias, _MODEL_ALIASES[_DEFAULT_ALIAS])
    prompt = question if not context else f"{question}\n\n--- CONTEXT ---\n{context}"
    timeout = float(os.getenv("SECOND_OPINION_TIMEOUT", "180"))
    max_chars = int(os.getenv("SECOND_OPINION_MAX_CHARS", "6000"))

    try:
        proc = await asyncio.create_subprocess_exec(
            agy, "-p", prompt, "--model", model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # binary vanished / not executable / OS error
        return f"⚠️ second_opinion failed to launch agy: {e}"

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return f"⚠️ second_opinion timed out after {int(timeout)}s (model: {model})."

    out = _strip_noise((out_b or b"").decode("utf-8", "replace"))
    if proc.returncode and not out:
        err = _strip_noise((err_b or b"").decode("utf-8", "replace"))
        return f"⚠️ second_opinion error (exit {proc.returncode}): {err[:500] or 'no output'}"
    if not out or out.startswith("Error: empty prompt"):
        return "⚠️ second_opinion returned no usable answer."
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n…[truncated at {max_chars} chars]"
    return f"[second opinion · {model}]\n\n{out}"


# JSON Schema for the tool input. `model` is an enum of aliases (NOT raw agy strings) so
# the model can't trigger agy's silent bad-model fallback; the handler validates anyway.
_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question / task to send to the other model.",
        },
        "model": {
            "type": "string",
            "enum": list(_MODEL_ALIASES.keys()),
            "description": "Which model to consult. flash=Gemini Flash (fast), "
                           "pro=Gemini 3.1 Pro (default, ~1M ctx), opus/sonnet=Claude via "
                           "Google, gpt=GPT-OSS 120B.",
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
    Validates input, coerces an unknown model alias to the default, then calls agy.
    Always returns an MCP text-content result; never raises."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"content": [{"type": "text",
                             "text": "⚠️ second_opinion needs a non-empty 'question'."}]}
    alias = (args.get("model") or _DEFAULT_ALIAS).strip().lower()
    if alias not in _MODEL_ALIASES:
        alias = _DEFAULT_ALIAS
    context = (args.get("context") or "").strip() or None
    text = await _ask_agy(question, alias, context)
    return {"content": [{"type": "text", "text": text}]}


def build_antigravity_server() -> dict | None:
    """Build the SDK MCP server exposing `second_opinion`, ready to drop into
    ClaudeAgentOptions(mcp_servers=...).

    Returns a ``{"antigravity": <server_config>}`` dict, or ``None`` when the feature is
    disabled or agy is unavailable (so the caller can pass ``mcp_servers=result or {}``).
    Call it once at engine import — building the server does not invoke agy; each tool
    call shells out fresh, so there is nothing to go stale.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except Exception:
        return None
    if not _enabled() or not _resolve_agy():
        return None

    @tool(
        "second_opinion",
        "Consult a DIFFERENT model (Google Gemini / GPT-OSS / Claude-via-Google) for an "
        "independent second opinion, billed to a separate Google quota — not the Anthropic "
        "budget. Use SPARINGLY, only when it genuinely helps: a cross-model sanity check "
        "before a risky or irreversible step, a long-context analysis (Gemini ~1M tokens), "
        "or offloading a bulky read/summary. NOT for routine steps. Returns the other "
        "model's answer as text.",
        _INPUT_SCHEMA,
    )
    async def second_opinion(args: dict) -> dict:
        return await _second_opinion_handler(args)

    server = create_sdk_mcp_server(name="antigravity", version="1.0.0", tools=[second_opinion])
    return {"antigravity": server}


# The stable tool name the agent sees: mcp__<server-key>__<tool-name>.
TOOL_NAME = "mcp__antigravity__second_opinion"
