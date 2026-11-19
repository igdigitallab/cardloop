"""spec-098 Forget: the Claude adapter (whole-session delete).

Owns every file Claude keeps for one session: the transcript `~/.claude/projects/<slug>/<sid>.jsonl`
and its `<sid>/` directory (subagents, tool-results, workflows), `file-history/<sid>`,
`session-env/<sid>`, `debug/<sid>.txt`, the same per-session directories under every
`~/.claude-accounts/<id>/` that has its own copy, and the `history.jsonl` lines of the session.
Paths come from `Path.home()` (tests point HOME at a tmp dir). Registered with `forget`.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

import forget
from forget import ContentItem, SessionArtifacts

_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")
_MAX_LINE = 64 << 20                 # a transcript line above this is skipped, never parsed
_TOOL_RESULT_CHUNK = 32 << 10
_TOOL_RESULT_CHUNKS = 4              # per file: windows need a few samples, not the whole output


def _home() -> Path:
    return Path.home()


def _slug(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _iso_ts(s: Any) -> "float | None":
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _config_dirs() -> "list[Path]":
    """`~/.claude` plus every `~/.claude-accounts/<id>`: each may own file-history/session-env."""
    out = [_home() / ".claude"]
    acc = _home() / ".claude-accounts"
    if acc.is_dir():
        out += sorted(p for p in acc.iterdir() if p.is_dir())
    return out


def _iter_lines(path: Path) -> Iterator[dict]:
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        for raw in f:
            if len(raw) > _MAX_LINE:
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def _blocks_text(content: Any) -> "list[tuple[str, str, Any]]":
    """(block type, text, raw block) for the text-bearing blocks of a message `content`."""
    if isinstance(content, str):
        return [("text", content, None)]
    out: list = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append(("text", str(b.get("text") or ""), b))
            elif t == "thinking":
                out.append(("thinking", str(b.get("thinking") or ""), b))
            elif t == "tool_use":
                out.append(("tool_use", json.dumps(b.get("input") or {}, ensure_ascii=False)[:20000], b))
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                out.append(("tool_result", str(c or ""), b))
    return out


def _items_from_record(r: dict) -> Iterator[ContentItem]:
    ts = _iso_ts(r.get("timestamp"))
    uuid = r.get("uuid") if isinstance(r.get("uuid"), str) else None
    rtype = r.get("type")
    if rtype == "queue-operation":
        c = r.get("content")
        if r.get("operation") == "enqueue" and isinstance(c, str) and c.strip():
            yield ContentItem("prompt", c, ts, uuid=None)
    elif rtype == "last-prompt":
        if isinstance(r.get("lastPrompt"), str):
            yield ContentItem("summary", r["lastPrompt"], ts)
    elif rtype == "ai-title":
        t = r.get("aiTitle") or r.get("title")
        if isinstance(t, str):
            yield ContentItem("summary", t, ts)
    elif rtype == "summary":
        if isinstance(r.get("summary"), str):
            yield ContentItem("summary", r["summary"], ts)
    elif rtype == "attachment":
        att = r.get("attachment")
        if isinstance(att, dict):
            for k in ("content", "stdout", "text"):
                if isinstance(att.get(k), str) and att[k].strip():
                    yield ContentItem("tool_result", att[k], ts)
    elif rtype in ("user", "assistant"):
        msg = r.get("message")
        if not isinstance(msg, dict):
            return
        for btype, text, raw in _blocks_text(msg.get("content")):
            if rtype == "user":
                if btype == "tool_result":
                    yield ContentItem("tool_result", text, ts, uuid=uuid)
                elif text.strip():
                    yield ContentItem("prompt", text, ts, uuid=uuid)
            elif btype == "tool_use":
                yield ContentItem("tool_use", text, ts, tool=str((raw or {}).get("name") or ""),
                                  tool_input=(raw or {}).get("input") if isinstance((raw or {}).get("input"), dict) else {},
                                  uuid=uuid)
            elif text.strip():
                yield ContentItem("assistant", text, ts, uuid=uuid)


class ClaudeAdapter:
    name = "claude"
    can_delete_session = True
    can_forget_turn = False            # phase P4

    # ── locate ──────────────────────────────────────────────────────────────────────────────
    def locate(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
               session_id: str) -> "SessionArtifacts | None":
        if not _SID_RE.match(session_id or ""):
            return None
        cwd = str(project.get("cwd") or "")
        slug_dir = _home() / ".claude" / "projects" / _slug(cwd)
        transcript = slug_dir / f"{session_id}.jsonl"
        sdir = slug_dir / session_id
        if not transcript.exists() and not sdir.exists():
            return None
        paths: list = [transcript, sdir]
        seen_real: set = set()
        for cfg in _config_dirs():
            for rel in (f"file-history/{session_id}", f"session-env/{session_id}",
                        f"debug/{session_id}.txt"):
                p = cfg / rel
                if not (p.exists() or p.is_symlink()):
                    continue
                try:
                    real = p.resolve()
                except OSError:
                    real = p
                if real in seen_real:
                    continue
                seen_real.add(real)
                paths.append(p)
        history_files: list = []
        seen_hist: set = set()
        for cfg in _config_dirs():
            h = cfg / "history.jsonl"
            try:
                real = h.resolve()
            except OSError:
                continue
            if real in seen_hist or not h.is_file():
                continue
            seen_hist.add(real)
            if self._history_has(h, session_id):
                history_files.append(str(h))
        first = last = None
        title = ""
        prompts = 0
        for r in _iter_lines(transcript):
            ts = _iso_ts(r.get("timestamp"))
            if ts is not None:
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            if r.get("type") == "ai-title":
                title = str(r.get("aiTitle") or r.get("title") or title)
            elif r.get("type") == "user":
                msg = r.get("message")
                if isinstance(msg, dict) and not any(
                        t == "tool_result" for t, _x, _y in _blocks_text(msg.get("content"))):
                    prompts += 1
        subs = 0
        sub_dir = sdir / "subagents"
        if sub_dir.is_dir():
            subs = sum(1 for _ in sub_dir.glob("*.jsonl"))
        labels = {}
        try:
            labels = json.loads((Path(ctx["DATA"]) / "session_labels.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        title = str(labels.get(session_id) or title or "")
        return SessionArtifacts(
            provider="claude", session_id=session_id, cwd=cwd, title=title, paths=paths,
            first_ts=first, last_ts=last,
            extra={"transcript": str(transcript), "session_dir": str(sdir),
                   "history_files": history_files, "residue_dirs": [str(slug_dir)],
                   "prompts": prompts, "subagents": subs})

    @staticmethod
    def _history_has(path: Path, session_id: str) -> bool:
        """Does a history.jsonl hold a line OF this session? The same predicate the rewrite uses: a
        raw substring test also hit another session's prompt that merely quotes the id ("claude
        --resume <id>"), so verify failed after the transcript was gone and the purge stuck."""
        needle = session_id.encode()
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if needle not in raw:
                        continue
                    try:
                        r = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("sessionId") == session_id:
                        return True
        except OSError:
            return False
        return False

    def fingerprint(self, art: SessionArtifacts) -> str:
        e = art.extra or {}
        return f"{e.get('prompts', 0)}:{e.get('subagents', 0)}"

    def sibling_roots(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> "list[Path]":
        """Where other sessions of the project quote an upload (their transcripts)."""
        return [Path(d) for d in (art.extra or {}).get("residue_dirs", [])]

    # ── busy ────────────────────────────────────────────────────────────────────────────────
    def busy_reason(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
                    session_id: str) -> "str | None":
        """A process other than the cockpit holding the transcript (or its dir) open for write, or
        a `claude --resume <sid>` in some terminal. The cockpit's own CLI children were evicted
        before this runs, so anything left here is an unknown writer: refuse."""
        art_paths = []
        cwd = str(project.get("cwd") or "")
        slug_dir = _home() / ".claude" / "projects" / _slug(cwd)
        art_paths = [str(slug_dir / f"{session_id}.jsonl"), str(slug_dir / session_id)]
        me = os.getpid()
        proc = Path("/proc")
        try:
            pids = [p for p in proc.iterdir() if p.name.isdigit()]
        except OSError:
            return None
        for p in pids:
            pid = int(p.name)
            if pid == me:
                continue
            try:
                cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                cmd = ""
            if session_id in cmd and "claude" in cmd:
                return f"a claude process ({pid}) is running this session"
            try:
                fds = list((p / "fd").iterdir())
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if not any(target == a or target.startswith(a + "/") for a in art_paths):
                    continue
                try:
                    info = (p / "fdinfo" / fd.name).read_text()
                    m = re.search(r"^flags:\s*(\d+)", info, re.M)
                    flags = int(m.group(1), 8) if m else 1
                except (OSError, ValueError):
                    flags = 1
                if flags & 3:
                    try:
                        comm = (p / "comm").read_text().strip()
                    except OSError:
                        comm = "?"
                    return f"process {pid} ({comm}) holds a session file open for writing"
        return None

    # ── content ─────────────────────────────────────────────────────────────────────────────
    def read_content(self, art: SessionArtifacts) -> Iterator[ContentItem]:
        transcript = Path((art.extra or {}).get("transcript") or art.paths[0])
        for r in _iter_lines(transcript):
            yield from _items_from_record(r)
        sdir = Path((art.extra or {}).get("session_dir") or "")
        if not sdir.is_dir():
            return
        for sub in sorted((sdir / "subagents").glob("*.jsonl")) if (sdir / "subagents").is_dir() else []:
            for r in _iter_lines(sub):
                yield from _items_from_record(r)
        for sub_dir in ("tool-results", "workflows"):
            d = sdir / sub_dir
            if not d.is_dir():
                continue
            for dp, _dns, fns in os.walk(d):
                for fn in sorted(fns):
                    try:
                        with open(os.path.join(dp, fn), "rb") as f:
                            for _ in range(_TOOL_RESULT_CHUNKS):
                                chunk = f.read(_TOOL_RESULT_CHUNK)
                                if not chunk:
                                    break
                                yield ContentItem("tool_result", chunk.decode("utf-8", "replace"))
                    except OSError:
                        continue

    # ── delete ──────────────────────────────────────────────────────────────────────────────
    def delete(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict:
        files = 0
        for p in art.paths:
            p = Path(p)
            try:
                if p.is_symlink() or p.is_file():
                    p.unlink()
                    files += 1
                elif p.is_dir():
                    n, _sz = forget._tree_size([p])
                    shutil.rmtree(p)
                    files += n
            except FileNotFoundError:
                continue
        removed = 0
        for h in (art.extra or {}).get("history_files", []):
            removed += self._rewrite_history(Path(h), art.session_id)
        return {"files": files, "history_lines": removed}

    @staticmethod
    def _rewrite_history(path: Path, session_id: str) -> int:
        """Drop the session's lines from a history.jsonl (atomic tmp + rename). Other CLIs append to
        it concurrently: the old inode stays open across the rename and whatever was appended to it
        after our read is copied onto the new file, so no other session's line is lost."""
        try:
            src = open(path, "rb")
        except OSError:
            return 0
        tmp = path.with_name(path.name + ".forget-tmp")
        with src:
            data = src.read()
            keep: list = []
            gone = 0
            for raw in data.splitlines(keepends=True):
                try:
                    r = json.loads(raw)
                except ValueError:
                    keep.append(raw)
                    continue
                if isinstance(r, dict) and r.get("sessionId") == session_id:
                    gone += 1
                else:
                    keep.append(raw)
            if not gone:
                return 0
            try:
                with open(tmp, "wb") as f:
                    f.writelines(keep)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, os.fstat(src.fileno()).st_mode & 0o777)
                os.replace(tmp, path)
                # lines appended to the old inode between our read and the rename
                src.seek(len(data))
                tail = src.read()
                if tail:
                    with open(path, "ab") as f:
                        f.write(tail)
                return gone
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return 0

    def verify_gone(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> "list[str]":
        left = [str(p) for p in art.paths if Path(p).exists() or Path(p).is_symlink()]
        for h in (art.extra or {}).get("history_files", []):
            if self._history_has(Path(h), art.session_id):
                left.append(h)
        return left

    def residue_roots(self, ctx: Mapping[str, Any]) -> "list[Path]":
        base = _home() / ".claude"
        return [base / "file-history", base / "session-env", base / "debug", base / "history.jsonl"]


forget.register_adapter(ClaudeAdapter())
