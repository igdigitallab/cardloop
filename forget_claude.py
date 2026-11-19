"""spec-098 Forget: the Claude adapter (whole-session delete and "forget from here").

Owns every file Claude keeps for one session: the transcript `~/.claude/projects/<slug>/<sid>.jsonl`
and its `<sid>/` directory (subagents, tool-results, workflows), `file-history/<sid>`,
`session-env/<sid>`, `debug/<sid>.txt`, the same per-session directories under every
`~/.claude-accounts/<id>/` that has its own copy, and the `history.jsonl` lines of the session.
Paths come from `Path.home()` (tests point HOME at a tmp dir). Registered with `forget`.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

import forget
import turn_marks
from forget import ContentItem, ForgetError, SessionArtifacts

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


# ── forget from here (P4): shared helpers ───────────────────────────────────────────────────────
_SNAPSHOT_TAG = b'"file-history-snapshot"'
_MIN_NAME = 6                        # a shorter artifact name is too common to look up as text
_HOLD_IN_MEMORY = 64 << 20           # the cut suffix above this is held in an unnamed temp file
NO_MARK_TEXT = ("This reply was made before forgetting was available (or its start was not recorded), "
                "so it cannot be cut out on its own. Delete the whole session instead.")
END_SLACK_SEC = 2.0


def _history_line_goes(r: dict, session_id: str, min_ts_ms: "float | None") -> bool:
    if r.get("sessionId") != session_id:
        return False
    if min_ts_ms is None:
        return True
    ts = r.get("timestamp")
    return isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= min_ts_ms


def _precise_ts(s: Any) -> "float | None":
    """An ISO transcript timestamp as epoch seconds WITH its milliseconds (a turn boundary is a
    question of a second or less)."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _is_prompt_line(r: dict) -> bool:
    """A turn start the operator side wrote: a user line with string content that the harness did
    not write (isMeta) and that is not a native compaction summary."""
    m = r.get("message")
    return (r.get("type") == "user" and r.get("isMeta") is not True
            and r.get("isCompactSummary") is not True
            and isinstance(m, dict) and isinstance(m.get("content"), str))


def _lines_from(path: Path, start: int, end: int) -> Iterator["tuple[int, bytes]"]:
    """(offset, raw line) of every line that starts in [start, end), streamed."""
    with open(path, "rb") as f:
        f.seek(start)
        off = start
        while off < end:
            raw = f.readline()
            if not raw:
                break
            yield off, raw
            off += len(raw)


def _decode(raw: bytes) -> "dict | None":
    if len(raw) > _MAX_LINE:
        return None
    try:
        r = json.loads(raw)
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


def _snapshot_backups(r: dict) -> "set[str]":
    snap = r.get("snapshot") if isinstance(r.get("snapshot"), dict) else {}
    out: set = set()
    for v in (snap.get("trackedFileBackups") or {}).values():
        if isinstance(v, dict) and isinstance(v.get("backupFileName"), str) and v["backupFileName"]:
            out.add(v["backupFileName"])
    return out


def _last_line_parses(path: Path, size: int) -> bool:
    """The kept prefix still ends in a whole JSON line (what the CLI resumes from)."""
    if size == 0:
        return True
    with open(path, "rb") as f:
        back = min(size, 1 << 20)
        f.seek(size - back)
        tail = f.read(back)
    if not tail.endswith(b"\n"):
        return False
    body = tail[:-1]
    nl = body.rfind(b"\n")
    if nl < 0 and back < size:
        return True              # one line longer than 1 MB: its end is a newline, good enough
    try:
        json.loads(body[nl + 1:])
        return True
    except ValueError:
        return False


def _claude_runs_session(argv: "list[str]", session_id: str) -> bool:
    """Is this command line a Claude CLI on the session? The program (argv[0], or the script a
    node runs) must be claude AND the id one of its ARGUMENTS (`--resume <sid>`, `--resume=<sid>`).
    A shell whose script text merely mentions the id next to a `~/.claude/...` path is not one:
    the substring test refused live deletes because of the deleting agent's own Bash command."""
    if not argv or not session_id:
        return False
    head = argv[:2]
    if not any(os.path.basename(a) == "claude" or "claude-code" in a for a in head):
        return False
    return any(a == session_id or a.endswith("=" + session_id) for a in argv[1:])


class ClaudeAdapter:
    name = "claude"
    can_delete_session = True
    can_forget_turn = True             # P4: truncate at a turn mark (see locate_cut)

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
                argv = [a.decode("utf-8", "replace") for a in (p / "cmdline").read_bytes().split(b"\0") if a]
            except OSError:
                argv = []
            if _claude_runs_session(argv, session_id):
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
    def _rewrite_history(path: Path, session_id: str, min_ts_ms: "float | None" = None) -> int:
        """Drop the session's lines from a history.jsonl (atomic tmp + rename); with `min_ts_ms`
        only its lines from that time on (forget from here). Other CLIs append to it concurrently:
        the old inode stays open across the rename and whatever was appended to it after our read
        is copied onto the new file, so no other session's line is lost."""
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
                if isinstance(r, dict) and _history_line_goes(r, session_id, min_ts_ms):
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

    # ── forget from here (P4) ───────────────────────────────────────────────────────────────
    def locate_cut(self, ctx: Mapping[str, Any], project: Mapping[str, Any], session_id: str,
                   anchor_uuid: "str | None", anchor_run_id: "str | None" = None
                   ) -> "tuple[SessionArtifacts, list[tuple[float | None, str]]]":
        """The turn that holds `anchor_uuid` (or IS run `anchor_run_id`: a row the feed rendered live
        has no transcript uuid yet, but knows its run) and every later one.
        -> (artifacts, the first few prompt lines of the cut as (ts, text): the caller picks the one
        that is really the operator's — a kept turn's late notification can land after the mark).

        The cut is the turn mark R with the greatest offset <= the anchor's line: the transcript's
        size when R's prompt was about to reach the CLI, i.e. byte for byte the prefix the CLI
        resumed from at that moment. The anchor must really be inside R (a later run that left no
        mark would otherwise drag R, a turn the operator did not pick, into the cut). Raises
        ForgetError: 404 unknown session, 400 `predecessor` (not in this session), 400 `no_mark`."""
        art = self.locate(ctx, project, session_id)
        if art is None:
            raise ForgetError(404, "not_found", "session not found")
        transcript = Path(art.extra["transcript"])
        sdir = Path(art.extra["session_dir"])
        try:
            size = transcript.stat().st_size
        except OSError:
            raise ForgetError(404, "not_found", "session not found") from None
        rows = turn_marks.read(Path(ctx["DATA"]), session_id)
        starts = [r for r in rows if r.get("run_id") and isinstance(r.get("offset"), int)
                  and not isinstance(r.get("offset"), bool) and 0 <= r["offset"] <= size]
        ends: dict = {}
        for r in rows:
            if r.get("run_id") and isinstance(r.get("ts_end"), (int, float)):
                ends[str(r["run_id"])] = float(r["ts_end"])
        a_ts: "float | None" = None
        if anchor_run_id:
            own = [r for r in starts if str(r["run_id"]) == anchor_run_id]
            if not own:
                raise ForgetError(400, "no_mark", "that turn has no recorded start in this chat's current "
                                                  "session, so it cannot be cut out on its own")
            owner = own[0]
            cut = int(owner["offset"])
        else:
            needle = str(anchor_uuid or "").encode()
            anchor_off: "int | None" = None
            anchor: dict = {}
            for off, raw in _lines_from(transcript, 0, size) if needle else ():
                if needle in raw:
                    r = _decode(raw)
                    if r is not None and r.get("uuid") == anchor_uuid:
                        anchor_off, anchor = off, r
                        break
            if anchor_off is None:
                raise ForgetError(400, "predecessor", "that message is not part of this chat's current session")
            cands = [r for r in starts if r["offset"] <= anchor_off]
            if not cands:
                raise ForgetError(400, "no_mark", NO_MARK_TEXT)
            cut = max(r["offset"] for r in cands)
            owner = max((r for r in cands if r["offset"] == cut), key=lambda r: float(r.get("ts_start") or 0))
            a_ts = _precise_ts(anchor.get("timestamp"))
            end = ends.get(str(owner["run_id"]))
            if end is not None and a_ts is not None:
                inside = a_ts <= end + END_SLACK_SEC
            else:
                starts_before = 0
                for _off, raw in _lines_from(transcript, cut, anchor_off):
                    if b'"user"' in raw:
                        r = _decode(raw)
                        if r is not None and _is_prompt_line(r):
                            starts_before += 1
                inside = starts_before == 0 if _is_prompt_line(anchor) else starts_before <= 1
            if not inside:
                raise ForgetError(400, "no_mark", NO_MARK_TEXT)
        if cut > 0:
            with open(transcript, "rb") as f:
                f.seek(cut - 1)
                if f.read(1) != b"\n":
                    raise ForgetError(400, "no_mark", NO_MARK_TEXT)
        dropped = sorted({str(r["run_id"]) for r in starts if r["offset"] >= cut})
        prev_end = max((ends.get(str(r["run_id"])) or 0.0 for r in starts if r["offset"] < cut), default=0.0)
        prev_start = max((float(r.get("ts_start") or 0.0) for r in starts if r["offset"] < cut), default=0.0)

        # the suffix: what goes, the tool calls that own sub-agents, the backups it snapshotted
        tool_ids: set = set()
        suffix_backups: set = set()
        prompts: list = []
        last_ts: "float | None" = None
        h = hashlib.sha256()
        for _off, raw in _lines_from(transcript, cut, size):
            h.update(raw)
            r = _decode(raw)
            if r is None:
                continue
            ts = _precise_ts(r.get("timestamp"))
            if ts is not None:
                last_ts = ts if last_ts is None else max(last_ts, ts)
            if r.get("type") == "assistant":
                content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        tool_ids.add(str(b["id"]))
            elif _SNAPSHOT_TAG in raw:
                suffix_backups |= _snapshot_backups(r)
            if len(prompts) < 5 and _is_prompt_line(r):
                prompts.append((ts, str(r["message"]["content"])))
        # the prefix: only what decides which shared artifacts stay
        prefix_backups: set = set()
        prefix_lines = 0
        for _off, raw in _lines_from(transcript, 0, cut):
            prefix_lines += 1
            if _SNAPSHOT_TAG in raw:
                r = _decode(raw)
                if r is not None:
                    prefix_backups |= _snapshot_backups(r)

        paths: list = []
        sub_dir = sdir / "subagents"
        sub_drop: list = []
        sub_keep: list = []
        if sub_dir.is_dir():
            for sub in sorted(sub_dir.glob("*.jsonl")):
                meta = sub.with_name(sub.name[:-len(".jsonl")] + ".meta.json")
                owner_tool = None
                try:
                    owner_tool = json.loads(meta.read_text(encoding="utf-8")).get("toolUseId")
                except (OSError, ValueError, AttributeError):
                    pass
                if owner_tool and str(owner_tool) in tool_ids:
                    sub_drop.append(sub)
                    paths.append(sub)
                    if meta.exists():
                        paths.append(meta)
                else:
                    sub_keep.append(sub)    # no owner, or owned by a kept turn: stays
        # tool-results / workflow files the dropped content names and the kept content does not
        named: dict = {}
        tr = sdir / "tool-results"
        if tr.is_dir():
            for f in sorted(tr.iterdir()):
                if len(f.name) >= _MIN_NAME:
                    named.setdefault(f.name, []).append(f)
        wf = sdir / "workflows"
        if wf.is_dir():
            for f in sorted(wf.iterdir()):
                if f.name.startswith("wf_"):
                    key = f.stem if f.suffix == ".json" else f.name
                    if len(key) >= _MIN_NAME:
                        named.setdefault(key, []).append(f)
        if named:
            names = list(named)
            used = forget.find_in_range(transcript, cut, size, names)
            for sub in sub_drop:
                used |= forget.find_in_range(sub, 0, None, names)
            kept = forget.find_in_range(transcript, 0, cut, sorted(used))
            for sub in sub_keep:
                kept |= forget.find_in_range(sub, 0, None, sorted(used - kept))
            for n in sorted(used - kept):
                paths.extend(named[n])
        # file-history backups only the dropped turns snapshotted (per account dir)
        seen_real: set = set()
        for b in sorted(suffix_backups - prefix_backups):
            if "/" in b or b.startswith("."):
                continue
            for cfg in _config_dirs():
                fp = cfg / "file-history" / session_id / b
                try:
                    real = fp.resolve()
                except OSError:
                    continue
                if real in seen_real or not fp.is_file():
                    continue
                seen_real.add(real)
                paths.append(fp)
        ts_start = float(owner.get("ts_start") or 0.0) or None
        first_ts = ts_start if ts_start is not None else a_ts
        last = max([x for x in [last_ts, first_ts, *[ends.get(d) for d in dropped]] if x is not None], default=None)
        extra = {
            "mode": "cut", "transcript": str(transcript), "session_dir": str(sdir), "cut": cut,
            "orig_size": size, "suffix_sha": h.hexdigest(), "dropped_runs": dropped,
            "prefix_lines": prefix_lines, "prev_end": prev_end or None, "prev_start": prev_start or None,
            "history_files": list(art.extra.get("history_files") or []),
            "history_cut_ms": (first_ts * 1000.0 - END_SLACK_SEC * 1000.0) if first_ts else None,
            "residue_dirs": list(art.extra.get("residue_dirs") or []),
            "sub_files": [str(x) for x in sub_drop],
        }
        cut_art = SessionArtifacts(provider="claude", session_id=session_id, cwd=art.cwd, title=art.title,
                                   paths=paths, first_ts=first_ts, last_ts=last, extra=extra)
        return cut_art, prompts

    def cut_fingerprint(self, art: SessionArtifacts) -> str:
        e = art.extra or {}
        return f"cut={e.get('cut')}:{','.join(e.get('dropped_runs') or [])}"

    def read_cut_content(self, art: SessionArtifacts) -> Iterator[ContentItem]:
        e = art.extra or {}
        t = Path(e["transcript"])
        for _off, raw in _lines_from(t, int(e["cut"]), int(e["orig_size"])):
            r = _decode(raw)
            if r is not None:
                yield from _items_from_record(r)
        for p in art.paths:
            p = Path(p)
            if p.name.endswith(".jsonl"):
                for r in _iter_lines(p):
                    yield from _items_from_record(r)
            elif p.parent.name in ("tool-results", "workflows") and p.is_file():
                try:
                    with open(p, "rb") as f:
                        for _ in range(_TOOL_RESULT_CHUNKS):
                            chunk = f.read(_TOOL_RESULT_CHUNK)
                            if not chunk:
                                break
                            yield ContentItem("tool_result", chunk.decode("utf-8", "replace"))
                except OSError:
                    continue

    def cut(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict:
        """Step 4 of B in one call (the pipeline calls the two halves itself, persisting in between
        that the truncate happened)."""
        n = 0 if (art.extra or {}).get("truncated") else self.cut_transcript(ctx, art)
        out = self.cut_artifacts(ctx, art)
        out["bytes_cut"] = n
        return out

    def cut_transcript(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> int:
        """Truncate the transcript at the cut. Truncates ONLY when the file is exactly what the plan
        measured (size and suffix hash); already cut = 0; anything else = 409 `changed`, nothing
        touched. A cut that does not verify is undone (500). Returns the bytes cut."""
        e = art.extra or {}
        t = Path(e["transcript"])
        cut, orig = int(e["cut"]), int(e["orig_size"])
        try:
            size = t.stat().st_size
        except OSError:
            raise ForgetError(500, "failed", "the transcript vanished during the forget") from None
        cut_bytes = 0
        if size != cut:
            if size != orig or self._suffix_sha(t, cut, orig) != e.get("suffix_sha"):
                raise ForgetError(409, "changed", "the session changed during the forget - nothing was cut")
            if cut > 0:
                with open(t, "rb") as f:
                    f.seek(cut - 1)
                    if f.read(1) != b"\n":
                        raise ForgetError(500, "failed", "the turn boundary is not a line end - nothing was cut")
            held = self._hold(t, cut, orig)
            try:
                with open(t, "r+b") as f:
                    os.truncate(f.fileno(), cut)
                    f.flush()
                    os.fsync(f.fileno())
                if t.stat().st_size != cut or not _last_line_parses(t, cut):
                    raise OSError("verify after truncate failed")
            except OSError as exc:
                self._restore(t, cut, held)
                raise ForgetError(500, "failed", f"the cut could not be verified and was undone ({exc})") from None
            finally:
                held.close()
            cut_bytes = orig - cut
        return cut_bytes

    def cut_artifacts(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict:
        """The dropped turns' artifacts and history.jsonl lines (idempotent)."""
        e = art.extra or {}
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
        for hf in e.get("history_files") or []:
            removed += self._rewrite_history(Path(hf), art.session_id, e.get("history_cut_ms"))
        return {"files": files, "history_lines": removed}

    @staticmethod
    def _suffix_sha(t: Path, cut: int, end: int) -> str:
        h = hashlib.sha256()
        with open(t, "rb") as f:
            f.seek(cut)
            left = end - cut
            while left > 0:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
        return h.hexdigest()

    @staticmethod
    def _hold(t: Path, cut: int, end: int):
        """The suffix about to be cut, kept until the cut verifies (in memory, or an unnamed temp
        file above 64 MB: an inode with no name that vanishes on close)."""
        holder = tempfile.SpooledTemporaryFile(max_size=_HOLD_IN_MEMORY)
        with open(t, "rb") as f:
            f.seek(cut)
            left = end - cut
            while left > 0:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    break
                holder.write(chunk)
                left -= len(chunk)
        holder.seek(0)
        return holder

    @staticmethod
    def _restore(t: Path, cut: int, held) -> None:
        try:
            held.seek(0)
            with open(t, "r+b") as f:
                f.truncate(cut)
                f.seek(cut)
                shutil.copyfileobj(held, f)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    def verify_cut(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> "list[str]":
        e = art.extra or {}
        t = Path(e["transcript"])
        left: list = []
        try:
            size = t.stat().st_size
            # once the truncate is recorded, a later turn may have appended (a resume after a crash)
            if size < int(e["cut"]) or (size != int(e["cut"]) and not e.get("truncated")):
                left.append(str(t))
        except OSError:
            left.append(str(t))
        left += [str(p) for p in art.paths if Path(p).exists() or Path(p).is_symlink()]
        for hf in e.get("history_files") or []:
            for r in _iter_lines(Path(hf)):
                if _history_line_goes(r, art.session_id, e.get("history_cut_ms")):
                    left.append(hf)
                    break
        return left


forget.register_adapter(ClaudeAdapter())
