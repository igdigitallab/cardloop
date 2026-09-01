#!/usr/bin/env python3
"""skills-lint — read-only audit of the Claude Code skill installation.

Why this exists: Claude Code skips a broken skill entry SILENTLY — no error, no
log line, nothing — so a dangling symlink just sits there uninvokable. On this
machine 63 of 87 skills were dangling symlinks into a directory that no longer
existed, for an unknown number of weeks, before anyone noticed (2026-09-01).
Separately, skills accumulate with no owner: not wired into the global core,
not scoped to any project, not parked as lazy — they just sit there and cost
tokens on every session that happens to load them.

Six checks, each reported with the offending names:
    1. dangling      — an entry that does not resolve to a readable SKILL.md.  ERROR
    2. frontmatter    — missing name:/description:, or name: != directory.     ERROR
    3. homeless       — loaded nowhere (not core, not a project, not lazy,
                         not a plugin). WARNING, with days-homeless from a
                         persisted first-seen state file.
    4. duplicates     — near-identical skills (content hash, -v1/-copy/-old
                         suffix, or overlap with an installed plugin's skill). WARNING
    5. stale content  — a path that doesn't exist on this machine, or an
                         outdated Claude model id. WARNING, file:line.
    6. cost           — approximate per-session token cost of the skill
                         DESCRIPTIONS actually loaded, per scope (global core +
                         each project's additions) — not one lump sum, since
                         that is the number that decides what stays.

Read-only by default. The only write path is --archive-homeless DAYS, which
moves skills homeless longer than DAYS out of ~/.claude/skills/ into
~/.claude/skills-archive/. It refuses (and reports, never silently skips)
anything that resolves through skills-lazy/ or an installed plugin.

Usage:
    skills-lint.py [--claude-dir DIR] [--repo REPO_ROOT] [--state FILE]
                   [--archive-homeless DAYS] [--json]

Exit code: 1 if any ERROR-level finding (dangling / bad frontmatter) — so a
cron job can alert on it. WARNING-level findings never fail the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Rough heuristic (no tokenizer dependency available offline) — every cost figure this
# tool prints is an approximation, labeled as such, not an exact SDK token count.
_CHARS_PER_TOKEN = 4

# current lineup (2026-09): Fable 5, Opus 5, Sonnet 5, Haiku 4.5. Anything naming an
# older generation in a skill file is a stale reference to something that no longer runs.
_STALE_MODEL_RE = re.compile(
    r"\bclaude-3(?:-[a-z0-9]+)*\b"
    r"|\bclaude-sonnet-4(?:-\d+)?\b"
    r"|\bclaude-opus-4(?:-\d+)?\b"
    r"|\bclaude-haiku-3(?:-\d+)?\b"
    r"|\bsonnet-4(?:\.\d+)?\b"
    r"|\bopus-4(?:\.\d+)?\b"
    r"|\bhaiku-3(?:\.\d+)?\b",
    re.IGNORECASE,
)

_PATH_RE = re.compile(r"(?:~|/home/[\w.\-]+)(?:/[\w.\-]+)+")

_DUP_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?:v\d+|old|copy|bak|backup)$", re.IGNORECASE)


# ─────────────────────────── env / config loading ────────────────────────────

def load_dotenv_merged(repo_root: Path) -> dict:
    """Mirror tools/doctor.py's _load_dotenv_merged(): .env fills gaps in the real
    process env, never overrides it, and this never mutates os.environ."""
    merged = dict(os.environ)
    env_path = repo_root / ".env"
    if env_path.exists() and not os.environ.get("COPS_NO_DOTENV"):
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged.setdefault(k.strip(), v.strip())
    return merged


def parse_core_allow(env: dict) -> "list[str] | None":
    """SKILLS_DEFAULT_ALLOW="a,b,c" — the global core. Unset means "no filter":
    the CLI default is every installed skill, and nothing is ever "homeless"."""
    raw = env.get("SKILLS_DEFAULT_ALLOW")
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


def load_topics(topics_file: Path) -> dict:
    if not topics_file.is_file():
        return {}
    try:
        data = json.loads(topics_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_project_skill_names(topics: dict) -> "set[str]":
    """Every skill name referenced by any project's agents_config.skills, with the
    additive "+name" prefix stripped. Being named here — additive or not — is
    evidence of intended ownership, so it counts for the homeless check either way."""
    names: "set[str]" = set()
    for proj in topics.values():
        if not isinstance(proj, dict):
            continue
        skills = (proj.get("agents_config") or {}).get("skills")
        if isinstance(skills, list):
            for s in skills:
                if isinstance(s, str) and s.strip():
                    names.add(s[1:].strip() if s.startswith("+") else s.strip())
    return names


def merge_project_skills(project_skills, default):
    """Ported verbatim from engine.py's _merge_project_skills — that function is the
    runtime source of truth for what the SDK actually loads; this is a read-only
    re-derivation for reporting, kept in sync by hand. See engine.py for the full
    rationale (additive "+name" syntax, degrade-to-default when there's nothing to
    add to)."""
    if project_skills is None or isinstance(project_skills, str):
        return project_skills if project_skills is not None else default
    plus = [s[1:].strip() for s in project_skills
            if isinstance(s, str) and s.startswith("+") and s[1:].strip()]
    if not plus:
        return project_skills
    if not isinstance(default, list):
        return default
    rest = [s for s in project_skills if isinstance(s, str) and not s.startswith("+")]
    merged, seen = [], set()
    for name in [*default, *plus, *rest]:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


# ─────────────────────────── frontmatter parsing ─────────────────────────────

def parse_frontmatter(text: str) -> "dict[str, str] | None":
    """Minimal SKILL.md frontmatter parser (mirrors webapp.py's _parse_skill_frontmatter,
    generalized to capture every key so callers can check more than name/description).
    Returns None if there is no '---\\n...\\n---' block at all — that itself is a
    frontmatter error the caller should report."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    block = text[4:end]
    out: "dict[str, str]" = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key:
            out[key] = val
    return out


# ─────────────────────────── skill directory scanning ────────────────────────

@dataclass
class SkillEntry:
    name: str
    path: Path
    ok: bool
    reason: str = ""
    skill_file: "Path | None" = None
    frontmatter: "dict[str, str] | None" = None
    body: str = ""


def scan_skills_dir(skills_dir: Path) -> "list[SkillEntry]":
    """Every entry under skills_dir, resolved or flagged dangling. A dangling entry
    is exactly what Claude Code silently skips: a broken symlink, a directory with
    no SKILL.md, or a SKILL.md that can't be read."""
    entries: "list[SkillEntry]" = []
    if not skills_dir.is_dir():
        return entries
    for item in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        name = item.name
        if item.is_symlink() and not item.exists():
            entries.append(SkillEntry(name, item, False, "broken symlink"))
            continue
        if not item.is_dir():
            entries.append(SkillEntry(name, item, False, "not a directory"))
            continue
        skill_file = None
        for candidate in ("SKILL.md", "skill.md"):
            p = item / candidate
            if p.is_file():
                skill_file = p
                break
        if skill_file is None:
            entries.append(SkillEntry(name, item, False, "no SKILL.md"))
            continue
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            entries.append(SkillEntry(name, item, False, f"unreadable SKILL.md: {e}"))
            continue
        if not text.strip():
            entries.append(SkillEntry(name, item, False, "empty SKILL.md"))
            continue
        fm = parse_frontmatter(text)
        entries.append(SkillEntry(name, item, True, "", skill_file, fm, text))
    return entries


# ─────────────────────────── plugins ─────────────────────────────────────────

def collect_plugin_skills(plugins_file: Path) -> "tuple[dict[str, str], dict[str, str]]":
    """Returns (name -> plugin id, name -> description) for every skill shipped by
    an installed plugin (scanned from <installPath>/skills/*/SKILL.md)."""
    names_to_plugin: "dict[str, str]" = {}
    names_to_desc: "dict[str, str]" = {}
    if not plugins_file.is_file():
        return names_to_plugin, names_to_desc
    try:
        data = json.loads(plugins_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return names_to_plugin, names_to_desc
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return names_to_plugin, names_to_desc
    for plugin_id, installs in plugins.items():
        if not isinstance(installs, list):
            continue
        seen_paths: "set[str]" = set()
        for inst in installs:
            install_path = inst.get("installPath") if isinstance(inst, dict) else None
            if not install_path or install_path in seen_paths:
                continue
            seen_paths.add(install_path)
            skills_subdir = Path(install_path) / "skills"
            if not skills_subdir.is_dir():
                continue
            for sub in sorted(skills_subdir.iterdir(), key=lambda p: p.name):
                if not sub.is_dir():
                    continue
                skill_file = None
                for candidate in ("SKILL.md", "skill.md"):
                    p = sub / candidate
                    if p.is_file():
                        skill_file = p
                        break
                if skill_file is None:
                    continue
                names_to_plugin[sub.name] = plugin_id
                try:
                    text = skill_file.read_text(encoding="utf-8", errors="replace")
                    fm = parse_frontmatter(text) or {}
                    names_to_desc[sub.name] = fm.get("description", "")
                except OSError:
                    pass
    return names_to_plugin, names_to_desc


def collect_lazy_names(lazy_dir: Path) -> "set[str]":
    if not lazy_dir.is_dir():
        return set()
    return {p.name for p in lazy_dir.iterdir() if p.is_dir()}


# ─────────────────────────── check 1 + 2: dangling / frontmatter ─────────────

def check_dangling(entries: "list[SkillEntry]") -> "list[dict]":
    return [{"name": e.name, "reason": e.reason} for e in entries if not e.ok]


def check_frontmatter(entries: "list[SkillEntry]") -> "list[dict]":
    out: "list[dict]" = []
    for e in entries:
        if not e.ok:
            continue
        fm = e.frontmatter or {}
        missing = [k for k in ("name", "description") if not fm.get(k)]
        if missing:
            out.append({"name": e.name, "issue": f"missing frontmatter field(s): {', '.join(missing)}"})
            continue
        if fm["name"] != e.name:
            out.append({"name": e.name, "issue": f"frontmatter name '{fm['name']}' does not match directory"})
    return out


# ─────────────────────────── check 3: homeless ────────────────────────────────

def compute_homeless_names(entries: "list[SkillEntry]", core_allow, project_names: "set[str]",
                            lazy_names: "set[str]", plugin_skills: "dict[str, str]") -> "list[str]":
    """A live, well-formed skill counts as homeless when it is in none of: the
    global core, any project's agents_config.skills, skills-lazy/, or an
    installed plugin. If core_allow is unset (None), every skill loads by
    default for every project — nothing is homeless in that state."""
    if core_allow is None:
        return []
    homeless = []
    core_set = set(core_allow)
    for e in entries:
        if not e.ok:
            continue
        name = e.name
        homed = (name in core_set or name in project_names
                 or name in lazy_names or name in plugin_skills)
        if not homed:
            homeless.append(name)
    return homeless


def load_state(state_file: Path) -> dict:
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def update_homeless_state(state: dict, homeless_names: "list[str]", today: date) -> dict:
    """New state: first-seen date persists for names still homeless, is recorded
    fresh for newly-homeless names, and is dropped for names no longer homeless
    (so a skill that regains an owner and later loses it again starts the clock
    over — "homeless for N days" should mean a continuous stretch)."""
    today_str = today.isoformat()
    return {name: state.get(name, today_str) for name in homeless_names}


def days_homeless(state: dict, name: str, today: date) -> int:
    first_seen = state.get(name)
    if not first_seen:
        return 0
    try:
        d = date.fromisoformat(first_seen)
    except ValueError:
        return 0
    return max(0, (today - d).days)


# ─────────────────────────── check 4: duplicates ──────────────────────────────

def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4:]
    return text


def compute_duplicates(entries: "list[SkillEntry]", plugin_skills: "dict[str, str]",
                        plugin_descriptions: "dict[str, str]",
                        desc_ratio: float = 0.6) -> "list[dict]":
    findings: "list[dict]" = []
    ok_entries = [e for e in entries if e.ok]

    # (a) exact-content duplicates: same body once frontmatter + whitespace are normalized.
    hash_map: "dict[str, list[str]]" = {}
    for e in ok_entries:
        norm = re.sub(r"\s+", " ", _strip_frontmatter(e.body)).strip().lower()
        if not norm:
            continue
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        hash_map.setdefault(h, []).append(e.name)
    for names in hash_map.values():
        if len(names) > 1:
            findings.append({"kind": "exact-content", "names": sorted(names)})

    # (b) name-suffix copies: "<base>-v1" / "-old" / "-copy" / "-bak" / "-backup" where
    # <base> also exists — e.g. design-taste-frontend-v1 next to design-taste-frontend.
    names_set = {e.name for e in ok_entries}
    for e in ok_entries:
        m = _DUP_SUFFIX_RE.match(e.name)
        if m and m.group("base") in names_set:
            findings.append({"kind": "suffix-copy", "names": sorted([e.name, m.group("base")])})

    # (c) exact-name overlap with an installed plugin's skill.
    for e in ok_entries:
        if e.name in plugin_skills:
            findings.append({"kind": "plugin-overlap", "names": [e.name],
                              "plugin": plugin_skills[e.name]})

    # (d) description-similarity overlap with a plugin skill under a different name
    # (skip names already caught by (c) — that's a stronger, exact signal).
    for e in ok_entries:
        if e.name in plugin_skills or not e.frontmatter:
            continue
        desc = (e.frontmatter.get("description") or "").strip().lower()
        if not desc:
            continue
        for pname, pdesc in plugin_descriptions.items():
            pdesc = (pdesc or "").strip().lower()
            if not pdesc:
                continue
            ratio = SequenceMatcher(None, desc, pdesc).ratio()
            if ratio >= desc_ratio:
                findings.append({"kind": "plugin-description-overlap",
                                  "names": [e.name, pname], "ratio": round(ratio, 2)})
    return findings


# ─────────────────────────── check 5: stale content ───────────────────────────

def scan_stale(entries: "list[SkillEntry]") -> "list[dict]":
    findings: "list[dict]" = []
    for e in entries:
        if not e.ok:
            continue
        for md in sorted(set(e.path.rglob("*.md")) | set(e.path.rglob("*.MD"))):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                rel = md.relative_to(e.path.parent)
            except ValueError:
                rel = md
            for lineno, line in enumerate(text.splitlines(), start=1):
                for m in _STALE_MODEL_RE.finditer(line):
                    findings.append({"skill": e.name, "file": str(rel), "line": lineno,
                                      "issue": f"stale model id: {m.group(0)}"})
                for m in _PATH_RE.finditer(line):
                    raw = m.group(0).rstrip(").,:;`'\"")
                    if "<" in raw or ">" in raw or "example" in raw.lower():
                        continue
                    expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
                    if not os.path.exists(expanded):
                        findings.append({"skill": e.name, "file": str(rel), "line": lineno,
                                          "issue": f"path does not exist: {raw}"})
    return findings


# ─────────────────────────── check 6: cost ────────────────────────────────────

def approx_tokens(chars: int) -> int:
    return chars // _CHARS_PER_TOKEN if chars else 0


def compute_cost(entries: "list[SkillEntry]", core_allow, topics: dict) -> "list[dict]":
    by_name = {e.name: e for e in entries if e.ok}

    def desc_of(name: str) -> str:
        e = by_name.get(name)
        if not e or not e.frontmatter:
            return ""
        return e.frontmatter.get("description", "")

    scopes: "list[dict]" = []

    if core_allow:
        chars = sum(len(desc_of(n)) for n in core_allow)
        scopes.append({"scope": "global core", "mode": "core", "count": len(core_allow),
                        "chars": chars, "approx_tokens": approx_tokens(chars)})
    else:
        chars = sum(len(desc_of(n)) for n in by_name)
        scopes.append({"scope": "global core", "mode": "unrestricted", "count": len(by_name),
                        "chars": chars, "approx_tokens": approx_tokens(chars),
                        "note": "SKILLS_DEFAULT_ALLOW is unset — every installed skill loads by default"})

    for proj_key, proj in sorted(topics.items()):
        if not isinstance(proj, dict):
            continue
        raw = (proj.get("agents_config") or {}).get("skills")
        if isinstance(raw, str):
            scopes.append({"scope": proj_key, "mode": "override",
                            "note": f"non-list skills override ({raw!r}) — cost not computed"})
            continue
        if not isinstance(raw, list) or not raw:
            continue
        plus = [s[1:].strip() for s in raw if isinstance(s, str) and s.startswith("+") and s[1:].strip()]
        rest = [s.strip() for s in raw if isinstance(s, str) and not s.startswith("+") and s.strip()]
        if plus and isinstance(core_allow, list):
            chars = sum(len(desc_of(n)) for n in plus)
            scopes.append({"scope": proj_key, "mode": "additive", "count": len(plus),
                            "chars": chars, "approx_tokens": approx_tokens(chars)})
        elif rest:
            effective = merge_project_skills(raw, core_allow)
            names = effective if isinstance(effective, list) else rest
            chars = sum(len(desc_of(n)) for n in names)
            scopes.append({"scope": proj_key, "mode": "replace", "count": len(names),
                            "chars": chars, "approx_tokens": approx_tokens(chars)})
        elif plus and not isinstance(core_allow, list):
            scopes.append({"scope": proj_key, "mode": "additive-noop",
                            "note": "core is unrestricted already — these '+' entries add nothing"})
    return scopes


# ─────────────────────────── write path: --archive-homeless ──────────────────

def is_protected(name: str, lazy_names: "set[str]", plugin_skills: "dict[str, str]") -> bool:
    """A skill that resolves through skills-lazy/ or an installed plugin must never
    be archived from ~/.claude/skills/ by this tool — it is either intentionally
    parked (lazy) or owned by a plugin's own lifecycle, not ours to move."""
    return name in lazy_names or name in plugin_skills


def archive_homeless(homeless: "list[dict]", skills_dir: Path, archive_dir: Path,
                      lazy_names: "set[str]", plugin_skills: "dict[str, str]",
                      days_threshold: int) -> dict:
    """Moves entries from `homeless` (list of {"name", "days"}) whose days meet the
    threshold into archive_dir. Refuses — and reports the refusal, never silently
    drops it — anything protected (see is_protected) or whose source under
    skills_dir is not a plain directory (e.g. a symlink)."""
    moved: "list[str]" = []
    refused: "list[dict]" = []
    skipped_below_threshold: "list[str]" = []

    for item in homeless:
        name = item["name"]
        days = item["days"]
        if days < days_threshold:
            skipped_below_threshold.append(name)
            continue
        if is_protected(name, lazy_names, plugin_skills):
            refused.append({"name": name, "reason": "resolves through skills-lazy or a plugin"})
            continue
        src = skills_dir / name
        if src.is_symlink() or not src.is_dir():
            refused.append({"name": name, "reason": "not a plain directory under skills_dir"})
            continue
        dest = archive_dir / name
        if dest.exists():
            refused.append({"name": name, "reason": f"archive destination already exists: {dest}"})
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved.append(name)

    return {"moved": moved, "refused": refused, "skipped_below_threshold": skipped_below_threshold}


# ─────────────────────────── orchestration ────────────────────────────────────

def run_lint(*, claude_dir: Path, repo_root: Path, state_file: Path,
             write_state: bool = True, today: "date | None" = None) -> dict:
    today = today or date.today()
    env = load_dotenv_merged(repo_root)
    core_allow = parse_core_allow(env)

    skills_dir = claude_dir / "skills"
    lazy_dir = claude_dir / "skills-lazy"
    plugins_file = claude_dir / "plugins" / "installed_plugins.json"
    topics_file = repo_root / "data" / "topics.json"

    entries = scan_skills_dir(skills_dir)
    topics = load_topics(topics_file)
    project_names = collect_project_skill_names(topics)
    lazy_names = collect_lazy_names(lazy_dir)
    plugin_skills, plugin_descriptions = collect_plugin_skills(plugins_file)

    dangling = check_dangling(entries)
    frontmatter_errors = check_frontmatter(entries)

    homeless_names = compute_homeless_names(entries, core_allow, project_names, lazy_names, plugin_skills)
    state = load_state(state_file)
    new_state = update_homeless_state(state, homeless_names, today)
    if write_state:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    homeless = sorted(
        ({"name": n, "days": days_homeless(new_state, n, today)} for n in homeless_names),
        key=lambda x: (-x["days"], x["name"]),
    )

    duplicates = compute_duplicates(entries, plugin_skills, plugin_descriptions)
    stale = scan_stale(entries)
    cost = compute_cost(entries, core_allow, topics)

    error_count = len(dangling) + len(frontmatter_errors)
    return {
        "dangling": dangling,
        "frontmatter_errors": frontmatter_errors,
        "homeless": homeless,
        "duplicates": duplicates,
        "stale": stale,
        "cost": cost,
        "meta": {
            "checked_at": today.isoformat(),
            "skills_dir": str(skills_dir),
            "total_skills": len(entries),
            "ok_skills": sum(1 for e in entries if e.ok),
            "core_allow_set": core_allow is not None,
            "error_count": error_count,
        },
    }


def render_report(report: dict) -> str:
    meta = report["meta"]
    lines = [
        "# skills-lint",
        "",
        f"Checked: {meta['checked_at']} — {meta['ok_skills']}/{meta['total_skills']} skills resolve, "
        f"{meta['error_count']} error(s).",
        "",
        "## 1. Dangling (ERROR)",
    ]
    if report["dangling"]:
        for d in report["dangling"]:
            lines.append(f"- {d['name']} — {d['reason']}")
    else:
        lines.append("none")

    lines += ["", "## 2. Frontmatter (ERROR)"]
    if report["frontmatter_errors"]:
        for f in report["frontmatter_errors"]:
            lines.append(f"- {f['name']} — {f['issue']}")
    else:
        lines.append("none")

    lines += ["", "## 3. Homeless (WARNING)"]
    if not meta["core_allow_set"]:
        lines.append("skipped — SKILLS_DEFAULT_ALLOW is unset, every skill loads by default")
    elif report["homeless"]:
        for h in report["homeless"]:
            lines.append(f"- {h['name']} — homeless for {h['days']} day(s)")
    else:
        lines.append("none")

    lines += ["", "## 4. Duplicates (WARNING)"]
    if report["duplicates"]:
        for d in report["duplicates"]:
            extra = ""
            if "plugin" in d:
                extra = f" (plugin: {d['plugin']})"
            if "ratio" in d:
                extra = f" (similarity: {d['ratio']})"
            lines.append(f"- [{d['kind']}] {', '.join(d['names'])}{extra}")
    else:
        lines.append("none")

    lines += ["", "## 5. Stale content (WARNING)"]
    if report["stale"]:
        for s in report["stale"]:
            lines.append(f"- {s['skill']} — {s['file']}:{s['line']} — {s['issue']}")
    else:
        lines.append("none")

    lines += ["", "## 6. Cost (per scope, approximate)"]
    for scope in report["cost"]:
        if "note" in scope and "chars" not in scope:
            lines.append(f"- {scope['scope']} ({scope['mode']}): {scope['note']}")
        else:
            note = f" — {scope['note']}" if "note" in scope else ""
            lines.append(f"- {scope['scope']} ({scope['mode']}, {scope['count']} skills): "
                         f"{scope['chars']} chars ≈ {scope['approx_tokens']} tokens{note}")

    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Lint the Claude Code skill installation (read-only by default).")
    ap.add_argument("--claude-dir", default=None,
                     help="skills root (default: $HOME/.claude)")
    ap.add_argument("--repo", default=str(REPO_ROOT),
                     help="cardloop repo root, for .env + data/topics.json (default: this script's repo)")
    ap.add_argument("--state", default=None,
                     help="homeless first-seen state file (default: <repo>/data/skills-lint-state.json)")
    ap.add_argument("--archive-homeless", type=int, default=None, metavar="DAYS",
                     help="move skills homeless longer than DAYS into skills-archive/ (the only write path)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    claude_dir = Path(args.claude_dir).expanduser().resolve() if args.claude_dir else Path.home() / ".claude"
    state_file = Path(args.state).resolve() if args.state else repo_root / "data" / "skills-lint-state.json"

    report = run_lint(claude_dir=claude_dir, repo_root=repo_root, state_file=state_file)

    if args.archive_homeless is not None:
        lazy_names = collect_lazy_names(claude_dir / "skills-lazy")
        plugin_skills, _ = collect_plugin_skills(claude_dir / "plugins" / "installed_plugins.json")
        result = archive_homeless(report["homeless"], claude_dir / "skills", claude_dir / "skills-archive",
                                   lazy_names, plugin_skills, args.archive_homeless)
        report["archive"] = result
        if not args.json:
            print(f"skills-lint: archived {len(result['moved'])} skill(s): {result['moved'] or 'none'}")
            if result["refused"]:
                print(f"skills-lint: refused {len(result['refused'])}: {result['refused']}")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report(report), end="")

    return 1 if report["meta"]["error_count"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
