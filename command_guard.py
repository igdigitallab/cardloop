"""Tokenizer-based classifier behind the last-resort Bash deny hook (engine.py, PreToolUse).

``classify_command(text)`` is a PURE function (stdlib only, never mutates anything, never runs
anything) that answers one question: does this Bash command contain one of the hard-coded deny
shapes?  It replaces the old regex-over-text matcher, which (a) was fooled by quoting and global
git options, (b) denied commands that merely MENTIONED a denied shape, and (c) went super-linear
on repeated tokens (24 KB of ``dd `` took 2.7 s inside the cockpit's event loop).

How it reads a command
----------------------
* A small shell lexer (quotes, backslashes, ``$(...)``, backticks, ``<(...)``, heredocs,
  comments, env-assignment prefixes, the separators ``; & | && || |& ;;`` and newlines, reserved
  words such as ``if/then/do/{``) turns the text into a list of simple commands.  Command
  substitutions are lexed IN PLACE and their commands join the same list, so
  ``echo "$(git reset --hard)"`` is judged like ``git reset --hard``.
* DATA IS NOT A COMMAND.  A rule only fires on a command's own argv.  The arguments of ``echo`` /
  ``printf`` / ``grep``, the ``-m`` / ``-F`` text of ``git commit``, comments and heredoc bodies
  that feed a non-shell command (``cat > f <<'EOF'``) are never inspected.
* Content handed to an INTERPRETER is parsed as a program again: ``bash|sh|zsh|... -c``,
  ``eval``, ``ssh host "<cmd>"``, ``su -c``, ``sudo -s``, ``env -S``, a heredoc / here-string /
  piped ``echo`` that feeds a shell, and the command behind the wrappers ``sudo doas env xargs
  nohup timeout nice time command exec setsid stdbuf ...`` and ``find -exec``.
* Rules are functions of argv (``git`` global options before the subcommand are skipped, short
  options are read as clusters, long options as git's unambiguous prefixes), not substrings.

Limits (fail closed for the NARROW list, never for everything)
---------------------------------------------------------------
Input above ``MAX_CHARS``, nesting deeper than ``MAX_DEPTH``, more lexing work than the shared
budget allows, or text the lexer cannot parse (unterminated quote) falls back to a coarse
scan: the text is cut at ``; & | newline ( ) `` and whitespace, quote characters are stripped
and the same argv rules run on a bounded window after every trigger word (``git``, ``rm``,
``chmod``, ``docker``, ``dd``, ``mkfs``, ...).  Nothing else is denied, and no step of either
path backtracks, so the cost is linear in the input.

Not covered (honest list): code written to a file and then run (``echo '...' > x.sh; sh x.sh``),
foreign interpreters (``python -c``, ``perl -e``), git aliases, variable-built command names.
This is a circuit breaker for the usual accidents, not a sandbox.
"""
from __future__ import annotations

import functools
import re
from typing import NamedTuple, Optional

MAX_CHARS = 262_144       # above this the lexer is not even started
MAX_DEPTH = 24            # nesting of $( ), backticks and re-parsed payloads (bash -c "...")
WORK_FACTOR = 8           # chars lexed over ALL nested programs <= WORK_FACTOR * len + 20_000
_CRUDE_WINDOW = 64        # tokens looked at after a trigger word in the fallback scan
_CRUDE_TRIGGERS = 4000    # trigger words that get the full window; later ones get the short one
_CRUDE_TAIL_WINDOW = 12


class Finding(NamedTuple):
    rule: str      # stable id, written to the audit log
    reason: str    # human-readable, shown to the agent


# Stable rule ids -> reason. Reasons for the pre-existing rules keep their historical wording.
RULES = {
    "fork-bomb": "fork bomb: exhausts host processes/memory",
    "rm-root-home": "rm -rf targeting the filesystem root or $HOME wipes the host",
    "chmod-root": "chmod -R on the filesystem root can lock out the whole host",
    "git-force-push-protected":
        "git push --force into master/main rewrites shared history irreversibly",
    "git-hard-reset": "git reset --hard discards uncommitted work irreversibly",
    "ssh-dir-mutation": "writing to or deleting ~/.ssh can lock out or leak host SSH access",
    "ssh-dir-chmod-open":
        "opening ~/.ssh permissions to group/other exposes the host's SSH keys",
    "docker-system-prune":
        "docker system prune deletes every unused image/volume/network on the host",
    "mkfs": "mkfs formats a filesystem and destroys existing data",
    "dd-block-device": "dd writing to a raw block device can overwrite the host disk",
    "git-skip-hooks":
        "skipping git hooks (--no-verify, or -n on commit/am) bypasses the pre-commit secret "
        "scan, the last barrier before a credential lands in the public history "
        "(a scan false positive goes into .secretscanignore)",
    "git-hookspath":
        "overriding core.hooksPath turns off the repository's git hooks, including the "
        "pre-commit secret scan (a scan false positive goes into .secretscanignore)",
    "skip-secret-scan":
        "SKIP_SECRET_SCAN switches off the pre-commit secret scan, the last barrier before a "
        "credential lands in the public history (a scan false positive goes into "
        ".secretscanignore)",
}


def _f(rule: str) -> Finding:
    return Finding(rule, RULES[rule])


class _Unparseable(Exception):
    """The lexer cannot (or may not) finish: the caller falls back to the coarse scan."""


class _Budget:
    __slots__ = ("left",)

    def __init__(self, n: int) -> None:
        self.left = n

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise _Unparseable("work budget exceeded")


# ───────────────────────────────────────── lexer ───────────────────────────────────────────────
_RUNWS = re.compile(r"""([^ \t\r\f\v\n'"\\$`;&|<>()]+)([ \t\r\f\v]*)""")
_WS = re.compile(r"[ \t\r\f\v]+")
_DQRUN = re.compile(r'[^"\\$`]+')
_PARAMRUN = re.compile(r"""[^}{\\'"$`]+""")
_ANSI_END = re.compile(r"(?:\\.|[^'\\])*'", re.S)
_BT_END = re.compile(r"(?:\\.|[^`\\])*`", re.S)
_ARITH_RUN = re.compile(r"""[^()$`'";\n\\]+""")
_BT_ESC = re.compile(r"\\([$`\\])")
_EXPANSION = re.compile(r"\\[\s\S]|\$\(|`")
_ANSI_ESC = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|[0-7]{1,3}|[\s\S])")
_ANSI_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
                "f": "\f", "v": "\v"}

# Reserved words dropped when they open a command: the next word is the command.
_DROP = frozenset({"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "{", "}",
                   "!", "esac", "coproc"})

_WRITE_REDIRS = frozenset({">", ">>", ">|", ">&", "&>", "&>>", "<>"})


class _Cmd:
    """One simple command: its words (quotes removed, variables NOT expanded), its redirects
    ``[op, word, heredoc_body, delimiter_was_quoted]`` and the previous stage of its pipeline."""
    __slots__ = ("words", "redirs", "pipe_from")

    def __init__(self, pipe_from: "Optional[_Cmd]" = None) -> None:
        self.words: "list[str]" = []
        self.redirs: "list[list]" = []
        self.pipe_from = pipe_from


@functools.lru_cache(maxsize=64)
def _heredoc_end(delim: str, strip_tabs: bool) -> "re.Pattern":
    return re.compile(r"^" + (r"\t*" if strip_tabs else "") + re.escape(delim) + r"\r?$", re.M)


def _decode_ansi(raw: str) -> str:
    def one(m: "re.Match") -> str:
        g = m.group(1)
        c = g[0]
        if c == "x" and len(g) > 1 or c == "u" and len(g) > 1:
            return chr(int(g[1:], 16))
        if c in "01234567":
            return chr(int(g, 8) & 0xFF)
        return _ANSI_SIMPLE.get(g, g)
    return _ANSI_ESC.sub(one, raw)


class _Lexer:
    def __init__(self, s: str, out: "list[_Cmd]", bud: _Budget, depth: int,
                 spans: "Optional[list]" = None, heredocs: "Optional[list]" = None) -> None:
        self.s = s
        self.n = len(s)
        self.out = out
        self.bud = bud
        self.depth = depth
        self.spans = spans if spans is not None else []       # quoted/comment/heredoc regions
        self.heredocs = heredocs if heredocs is not None else []   # waiting for their body

    # -- nested constructs ---------------------------------------------------------------------
    def sub(self, pos: int, dollar: bool = True) -> int:
        """Lex a `$(`, `<(` or `>(` region in place; returns the index after its `)`.
        `$((` is arithmetic when it closes with `))` (its `<<` is a shift, not a heredoc)."""
        if self.depth >= MAX_DEPTH:
            raise _Unparseable("nesting too deep")
        if dollar and self.s.startswith("(", pos):
            end = self.arith(pos + 1, False)
            if end >= 0:
                return end
        child = _Lexer(self.s, self.out, self.bud, self.depth + 1, self.spans, self.heredocs)
        return child.run(pos, True)

    def arith(self, pos: int, semis: bool) -> int:
        """`$(( ... ))` / `(( ... ))`: `pos` is just after the opening pair. Returns the index
        after the closing `))`, or -1 when the text is not arithmetic (then it is parsed as
        nested subshells). Two phases keep the work linear: a flat scan decides whether the
        text IS arithmetic (no recursion, so a failed guess costs one pass); only then are the
        commands inside `$(...)` / backticks lexed, with `<` and `>` as plain characters."""
        end = self._arith_end(pos, semis)
        if end < 0:
            return -1
        content = self.s[pos:end - 2]
        if self.depth >= MAX_DEPTH:
            raise _Unparseable("nesting too deep")
        self.bud.spend(len(content) + 1)
        _Lexer(content, self.out, self.bud, self.depth + 1).run(0, False, True)
        return end

    def _arith_end(self, pos: int, semis: bool) -> int:
        s, n = self.s, self.n
        i, depth = pos, 2
        result = -1
        while i < n:
            m = _ARITH_RUN.match(s, i)
            if m:
                i = m.end()
                if i >= n:
                    break
            c = s[i]
            if c == "(":
                depth += 1
                i += 1
            elif c == ")":
                depth -= 1
                if depth == 1:                      # first of the closing pair
                    if s.startswith(")", i + 1):
                        result = i + 2
                    break
                i += 1
            elif c == "$":
                i += 1
            elif c == "`":
                j = s.find("`", i + 1)
                if j < 0:
                    break
                i = j + 1
            elif c == "'" or c == '"':
                j = s.find(c, i + 1)
                if j < 0:
                    break
                i = j + 1
            elif c == "\\":
                i += 2
            elif c == ";" and semis:
                i += 1
            else:                                   # newline, or `;` in `$(( ))`
                break
        self.bud.spend(min(i, n) - pos + 1)
        return result

    def backtick(self, pos: int) -> int:
        m = _BT_END.match(self.s, pos + 1)
        if m is None:
            raise _Unparseable("unterminated backtick")
        code = _BT_ESC.sub(r"\1", self.s[pos + 1:m.end() - 1])
        self._program(code)
        return m.end()

    def _program(self, code: str) -> None:
        if self.depth >= MAX_DEPTH:
            raise _Unparseable("nesting too deep")
        self.bud.spend(len(code) + 1)
        _Lexer(code, self.out, self.bud, self.depth + 1).run(0, False)

    def param(self, pos: int) -> "tuple[str, int]":
        """`${...}` (literal text kept, e.g. `${HOME}`); commands inside it are collected."""
        s, n = self.s, self.n
        i, depth = pos + 2, 1
        while i < n:
            m = _PARAMRUN.match(s, i)
            if m:
                i = m.end()
                if i >= n:
                    break
            c = s[i]
            if c == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return s[pos:i], i
            elif c == "{":
                depth += 1
                i += 1
            elif c == "\\":
                i += 2
            elif c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise _Unparseable("unterminated quote")
                i = j + 1
            elif c == '"':
                _t, i = self.dq(i + 1)
            elif c == "$" and s.startswith("$(", i):
                i = self.sub(i + 2)
            elif c == "`":
                i = self.backtick(i)
            else:
                i += 1
        raise _Unparseable("unterminated ${")

    def dq(self, pos: int) -> "tuple[str, int]":
        """Body of a double-quoted string starting at `pos` (just after the opening quote)."""
        s, n = self.s, self.n
        start = pos - 1
        buf: "list[str]" = []
        while True:
            m = _DQRUN.match(s, pos)
            if m:
                buf.append(m.group())
                pos = m.end()
            if pos >= n:
                raise _Unparseable("unterminated double quote")
            c = s[pos]
            if c == '"':
                pos += 1
                break
            if c == "\\":
                nx = s[pos + 1:pos + 2]
                if nx and nx in '$`"\\':
                    buf.append(nx)
                    pos += 2
                elif nx == "\n":
                    pos += 2
                else:
                    buf.append("\\")
                    pos += 1
            elif c == "$":
                nx = s[pos + 1:pos + 2]
                if nx == "(":
                    pos = self.sub(pos + 2)
                    buf.append("$()")
                elif nx == "{":
                    text, pos = self.param(pos)
                    buf.append(text)
                else:
                    buf.append("$")
                    pos += 1
            else:                                  # backtick
                pos = self.backtick(pos)
                buf.append("`")
        self.spans.append((start, pos))
        return "".join(buf), pos

    def ansi(self, pos: int) -> "tuple[str, int]":
        m = _ANSI_END.match(self.s, pos)
        if m is None:
            raise _Unparseable("unterminated $'")
        self.spans.append((pos - 2, m.end()))
        return _decode_ansi(self.s[pos:m.end() - 1]), m.end()

    def consume_heredocs(self, pos: int) -> int:
        """Read the bodies of the heredocs opened on the line that just ended."""
        s, n = self.s, self.n
        for hd in self.heredocs:
            m = _heredoc_end(hd[1] or "", hd[0] == "<<-").search(s, pos)
            if m is None:
                body, end = s[pos:], n
            else:
                body, end = s[pos:m.start()], m.end()
                if end < n and s[end] == "\n":
                    end += 1
            hd[2] = body
            self.spans.append((pos, end))
            if not hd[3] and ("$(" in body or "`" in body):
                # an unquoted delimiter: the body is expanded, so its substitutions RUN
                self.bud.spend(len(body) + 1)
                _Lexer(body, self.out, self.bud, self.depth + 1).expansions_only()
            pos = end
        self.heredocs.clear()
        return pos

    def expansions_only(self) -> None:
        if self.depth >= MAX_DEPTH:
            raise _Unparseable("nesting too deep")
        s, pos = self.s, 0
        while True:
            m = _EXPANSION.search(s, pos)
            if m is None:
                return
            t = m.group()
            if t == "$(":
                pos = self.sub(m.end())
            elif t == "`":
                pos = self.backtick(m.start())
            else:
                pos = m.end()

    # -- the main loop -------------------------------------------------------------------------
    def run(self, pos: int, until_paren: bool, arith: bool = False) -> int:
        s, n, out = self.s, self.n, self.out
        spans, heredocs = self.spans, self.heredocs
        parts: "list[str]" = []
        inword = False
        quoted = False
        cmd = _Cmd()
        pending: "Optional[list]" = None      # a redirect operator waiting for its target word
        cases: "list[str]" = []               # case state: subject / pattern / body
        paren = 0
        fn_name_next = False

        def flush_word() -> None:
            nonlocal inword, quoted, pending, fn_name_next
            if not inword:
                return
            w = parts[0] if len(parts) == 1 else "".join(parts)
            parts.clear()
            q = quoted
            inword = quoted = False
            if pending is not None:
                pending[1] = w
                if pending[0] == "<<" or pending[0] == "<<-":
                    pending[3] = q
                    heredocs.append(pending)
                pending = None
                return
            words = cmd.words
            if not words and not q:
                if fn_name_next:
                    fn_name_next = False
                    return
                if w in _DROP:
                    if w == "esac" and cases and cases[-1] != "subject":
                        cases.pop()
                    return
                if w == "function":
                    fn_name_next = True
                    return
                if w == "case":
                    cases.append("subject")      # `for x in a b` / `case x in` headers: their
                                                 # first word (`for`, `case`) is no known command
            elif cases and cases[-1] == "subject" and w == "in" and not q:
                cases[-1] = "pattern"
            words.append(w)

        def end_cmd(pipe: bool = False) -> None:
            nonlocal cmd, pending
            flush_word()
            pending = None
            nxt = None
            if cmd.words or cmd.redirs:
                out.append(cmd)
                if pipe:
                    nxt = cmd
            cmd = _Cmd(nxt)

        while pos < n:
            m = _RUNWS.match(s, pos)
            if m is not None:
                chunk = m.group(1)
                if chunk[0] == "#" and not inword:
                    e = s.find("\n", pos)
                    if e < 0:
                        e = n
                    spans.append((pos, e))
                    pos = e
                    continue
                parts.append(chunk)
                inword = True
                pos = m.end()
                if m.end(1) != pos:          # whitespace follows: the word is complete
                    flush_word()
                continue
            c = s[pos]
            if c in " \t\r\f\v":
                flush_word()
                pos = _WS.match(s, pos).end()
            elif c == "\n":
                end_cmd()
                pos += 1
                if heredocs:
                    pos = self.consume_heredocs(pos)
            elif c == "'":
                j = s.find("'", pos + 1)
                if j < 0:
                    raise _Unparseable("unterminated single quote")
                parts.append(s[pos + 1:j])
                inword = quoted = True
                spans.append((pos, j + 1))
                pos = j + 1
            elif c == '"':
                text, pos = self.dq(pos + 1)
                parts.append(text)
                inword = quoted = True
            elif c == "\\":
                nx = s[pos + 1:pos + 2]
                if nx == "\n":
                    pos += 2                  # line continuation
                elif nx == "":
                    parts.append("\\")
                    inword = True
                    pos += 1
                else:
                    parts.append(nx)
                    inword = quoted = True
                    pos += 2
            elif c == "$":
                nx = s[pos + 1:pos + 2]
                if nx == "(":
                    pos = self.sub(pos + 2)
                    parts.append("$()")
                elif nx == "{":
                    text, pos = self.param(pos)
                    parts.append(text)
                elif nx == "'":
                    text, pos = self.ansi(pos + 2)
                    parts.append(text)
                    quoted = True
                elif nx == '"':
                    text, pos = self.dq(pos + 2)
                    parts.append(text)
                    quoted = True
                else:
                    parts.append("$")
                    pos += 1
                inword = True
            elif c == "`":
                pos = self.backtick(pos)
                parts.append("`")
                inword = True
            elif c == ";":
                if s.startswith(";;&", pos):
                    pos += 3
                    if cases and cases[-1] == "body":
                        cases[-1] = "pattern"
                elif s.startswith(";;", pos) or s.startswith(";&", pos):
                    pos += 2
                    if cases and cases[-1] == "body":
                        cases[-1] = "pattern"
                else:
                    pos += 1
                end_cmd()
            elif c == "|":
                if s.startswith("||", pos):
                    pos += 2
                    end_cmd()
                elif s.startswith("|&", pos):
                    pos += 2
                    end_cmd(True)
                else:
                    pos += 1
                    if cases and cases[-1] == "pattern":      # `a|b)` — alternatives, not a pipe
                        flush_word()
                        cmd = _Cmd()
                        pending = None
                    else:
                        end_cmd(True)
            elif c == "&":
                if s.startswith("&&", pos):
                    pos += 2
                    end_cmd()
                elif s.startswith("&>", pos):
                    flush_word()
                    op = "&>>" if s.startswith("&>>", pos) else "&>"
                    pending = [op, None, None, False]
                    cmd.redirs.append(pending)
                    pos += len(op)
                else:
                    pos += 1
                    end_cmd()
            elif (c == "<" or c == ">") and arith:       # arithmetic: shifts and comparisons
                parts.append(c)
                inword = True
                pos += 1
            elif c == "<" or c == ">":
                nx = s[pos + 1:pos + 2]
                if nx == "(":                              # process substitution
                    pos = self.sub(pos + 2, False)
                    parts.append("<()")
                    inword = True
                    continue
                if inword and not quoted and len(parts) == 1 and parts[0].isdigit():
                    parts.clear()                          # `2>file`: the digit is an fd
                    inword = False
                else:
                    flush_word()
                if c == "<":
                    if s.startswith("<<<", pos):
                        op = "<<<"
                    elif s.startswith("<<-", pos):
                        op = "<<-"
                    elif nx == "<":
                        op = "<<"
                    elif nx == "&":
                        op = "<&"
                    elif nx == ">":
                        op = "<>"
                    else:
                        op = "<"
                elif nx == ">":
                    op = ">>"
                elif nx == "&":
                    op = ">&"
                elif nx == "|":
                    op = ">|"
                else:
                    op = ">"
                pos += len(op)
                pending = [op, None, None, False]
                cmd.redirs.append(pending)
            elif c == "(":
                flush_word()
                if s.startswith("((", pos) and not cmd.words and not cmd.redirs:
                    end = self.arith(pos + 2, True)     # `(( i < 5 ))`, `(( x = 1 << 3 ))`
                    if end >= 0:
                        pos = end
                        continue
                if not (cases and cases[-1] == "pattern"):
                    end_cmd()
                    paren += 1
                pos += 1
            else:                                          # ")"
                pos += 1
                if cases and cases[-1] == "pattern":
                    flush_word()
                    cmd = _Cmd()
                    pending = None
                    cases[-1] = "body"
                elif paren > 0:
                    end_cmd()
                    paren -= 1
                else:
                    end_cmd()
                    if until_paren:
                        return pos
        end_cmd()
        return n


# ─────────────────────────────────────── fork bomb ─────────────────────────────────────────────
_FN_OPEN = re.compile(r"\(\s*\)\s*\{")
_FN_NAME = re.compile(r"([^\s;&|(){}<>'\"`]{1,40})\s*$")


def _forkbomb(text: str, spans: "list[tuple[int, int]]") -> "Optional[Finding]":
    """``name(){ name|name& };name`` in the CODE part of the text (quotes, comments and heredoc
    bodies are blanked first, so a quoted mention is data)."""
    if "{" not in text or "&" not in text or "|" not in text or "(" not in text:
        return None
    if spans:
        spans = sorted(spans)
        pieces, last = [], 0
        for a, b in spans:
            if a >= last:
                pieces.append(text[last:a])
                pieces.append(" ")
                last = b
            elif b > last:
                last = b
        pieces.append(text[last:])
        text = "".join(pieces)
    checked = 0
    for m in _FN_OPEN.finditer(text):
        checked += 1
        if checked > 64:
            break
        nm = _FN_NAME.search(text, max(0, m.start() - 48), m.start())
        if nm is None:
            continue
        esc = re.escape(nm.group(1))
        window = text[m.end():m.end() + 400]
        if re.search(esc + r"\s*\|\s*" + esc + r"\s*&[^}]{0,200}\}\s*;\s*" + esc, window):
            return _f("fork-bomb")
    return None


# ─────────────────────────────────────── rule helpers ──────────────────────────────────────────
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_ROOTISH = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/)/*\**")
_SSH_PATH = re.compile(r"(?:~|\$\{?HOME\}?)/\.ssh(?![A-Za-z0-9_])")
_ROOT_MODE = re.compile(r"777|000|a[+=]rwx")
_OPEN_OCTAL = re.compile(r"[0-7]{2,3}[1-7]")
_OPEN_SYMBOLIC = re.compile(r"[ugoa]*[+=][^\s]*[rwx]")
_DD_OF = re.compile(r"of=/dev/(?!null\b|zero\b)")
_MKFS = re.compile(r"mkfs(?:\.\w+)?")
_DOCKER_VALUE = frozenset({"-H", "--host", "-c", "--context", "-l", "--log-level", "--config",
                           "--tlscacert", "--tlscert", "--tlskey"})
_SSH_VERBS = frozenset({"rm", "rmdir", "mv", "shred", "truncate", "chown", "tee"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "csh", "tcsh", "fish",
                     "rbash"})


def _cmd_name(w: str) -> str:
    return w.rpartition("/")[2]


def _check_assignment(word: str) -> "Optional[Finding]":
    """`NAME=value` as a prefix assignment, an `export`/`env` operand, ..."""
    eq = word.find("=")
    name = word[:eq].rstrip("+")
    val = word[eq + 1:]
    if name == "SKIP_SECRET_SCAN":
        if val != "":
            return _f("skip-secret-scan")
    elif name.startswith("GIT_CONFIG_"):
        low = val.lower()
        if (name.startswith("GIT_CONFIG_KEY_") and low.strip() == "core.hookspath") or \
                (name == "GIT_CONFIG_PARAMETERS" and "core.hookspath" in low):
            return _f("git-hookspath")
    return None


# -- git ---------------------------------------------------------------------------------------
_GIT_GLOBAL_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                               "--attr-source"})
# Subcommands that take --no-verify (git 2.47 `git <sub> -h`; cherry-pick does not today, but
# a hook-skipping flag there could only ever be an attempt, so it is denied too).
_NOVERIFY_SUBS = frozenset({"commit", "push", "merge", "am", "cherry-pick", "rebase"})
# `-n` IS --no-verify only for these two: `git commit -n` and `git am -n` ("bypass pre-applypatch
# and applypatch-msg hooks", see `git am -h`). Everywhere else it is something harmless:
# push -n = --dry-run, merge/rebase -n = --no-stat, cherry-pick -n = --no-commit.
_N_IS_NOVERIFY = frozenset({"commit", "am"})
# per subcommand: (long options taking a separate value, short options taking a value,
#                  short options whose value is optional and must be stuck to the flag)
_GIT_OPTS = {
    "commit": (frozenset({"--message", "--file", "--reuse-message", "--reedit-message",
                          "--author", "--date", "--template", "--fixup", "--squash",
                          "--cleanup", "--trailer", "--pathspec-from-file"}),
               frozenset("mFCct"), frozenset("uS")),
    "push": (frozenset({"--push-option", "--receive-pack", "--exec", "--repo"}),
             frozenset("o"), frozenset()),
    "merge": (frozenset({"--message", "--file", "--strategy", "--strategy-option",
                         "--into-name", "--cleanup"}),
              frozenset("mFsX"), frozenset("S")),
    "am": (frozenset({"--whitespace", "--directory", "--exclude", "--include", "--patch-format",
                      "--resolvemsg", "--empty", "--quoted-cr"}),
           frozenset("Cp"), frozenset("S")),
    "cherry-pick": (frozenset({"--mainline", "--strategy", "--strategy-option"}),
                    frozenset("mX"), frozenset("S")),
    "rebase": (frozenset({"--onto", "--strategy", "--strategy-option", "--exec", "--whitespace"}),
               frozenset("sXxC"), frozenset("S")),
}
_CFG_READ = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--unset",
                       "--unset-all", "--list", "-l", "--remove-section", "--rename-section",
                       "--edit", "-e", "--get-color", "--get-colorbool"})
_CFG_VALUE = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment"})


def _is_hookspath_key(v: str) -> bool:
    return v.split("=", 1)[0].strip().lower() == "core.hookspath"


def _is_noverify_long(t: str) -> bool:
    # git accepts any unambiguous prefix of a long option. Prefixes shorter than `--no-veri`
    # collide with --no-verbose and git rejects them, so denying from `--no-v` up costs nothing.
    return len(t) >= 6 and "--no-verify".startswith(t)


def _git_noverify(sub: str, rest: "list[str]") -> "Optional[Finding]":
    long_val, short_val, short_opt = _GIT_OPTS[sub]
    n_flag = sub in _N_IS_NOVERIFY
    skip = False
    for t in rest:
        if skip:
            skip = False
            continue
        if t == "--":
            break
        if len(t) < 2 or t[0] != "-":
            continue
        if t[1] == "-":
            if _is_noverify_long(t):
                return _f("git-skip-hooks")
            if t in long_val:
                skip = True
            continue
        body = t[1:]
        last = len(body) - 1
        for k, ch in enumerate(body):
            if ch == "n" and n_flag:
                return _f("git-skip-hooks")
            if ch in short_val:                 # the rest of the cluster (or the next word) is its value
                skip = k == last
                break
            if ch in short_opt:                 # optional value, stuck to the flag
                break
    return None


def _git_push_force(rest: "list[str]") -> "Optional[Finding]":
    force = lease = remote_by_flag = False
    pos: "list[str]" = []
    opts = True
    i, n = 0, len(rest)
    while i < n:
        t = rest[i]
        i += 1
        if opts and t == "--":
            opts = False
            continue
        if opts and len(t) > 1 and t[0] == "-":
            if t[1] == "-":
                name, eq, _v = t.partition("=")
                if name == "--force":
                    force = True
                elif name == "--force-with-lease":
                    lease = True
                elif name == "--repo":
                    remote_by_flag = True
                    if not eq:
                        i += 1
                elif name in ("--push-option", "--receive-pack", "--exec") and not eq:
                    i += 1
            else:
                last = len(t) - 1
                for k in range(1, len(t)):
                    ch = t[k]
                    if ch == "f":
                        force = True
                    elif ch == "o":                 # `-o <option>`: value, not more flags
                        if k == last:
                            i += 1
                        break
            continue
        pos.append(t)
    refspecs = pos if remote_by_flag else pos[1:]
    if not refspecs:
        # an unqualified forced push targets the current branch, which in this workflow
        # (one branch, master) is the protected one
        return _f("git-force-push-protected") if (force or lease) else None
    for r in refspecs:
        plus = r[0] == "+" and len(r) > 1
        if not (force or lease or plus):
            continue
        body = r[1:] if r[0] == "+" else r
        dst = body.partition(":")[2] if ":" in body else body
        if dst.startswith("refs/heads/"):
            dst = dst[11:]
        if dst == "master" or dst == "main":
            return _f("git-force-push-protected")
    return None


def _git_config(rest: "list[str]") -> "Optional[Finding]":
    pos: "list[str]" = []
    read = False
    i, n = 0, len(rest)
    while i < n:
        t = rest[i]
        i += 1
        if t == "--":
            pos.extend(rest[i:])
            break
        if len(t) > 1 and t[0] == "-":
            if t in _CFG_READ:
                read = True
            elif t in _CFG_VALUE:
                i += 1
            continue
        pos.append(t)
    if read:
        return None
    if pos and pos[0] == "set":                 # `git config set <key> <value>` (git >= 2.46)
        pos = pos[1:]
    elif pos and pos[0] in ("get", "unset", "list", "edit", "rename-section", "remove-section"):
        return None
    if len(pos) >= 2 and _is_hookspath_key(pos[0]):
        return _f("git-hookspath")
    return None


def _h_git(args: "list[str]") -> "Optional[Finding]":
    i, n = 0, len(args)
    hookspath = False
    while i < n:                                 # global options before the subcommand
        t = args[i]
        if t == "-c" or t == "--config-env":
            if i + 1 < n and _is_hookspath_key(args[i + 1]):
                hookspath = True
            i += 2
        elif t.startswith("--config-env="):
            if _is_hookspath_key(t[13:]):
                hookspath = True
            i += 1
        elif t in _GIT_GLOBAL_VALUE:
            i += 2
        elif len(t) > 1 and t[0] == "-":
            i += 1
        else:
            break
    if hookspath:
        return _f("git-hookspath")
    if i >= n:
        return None
    sub, rest = args[i], args[i + 1:]
    if sub == "config":
        return _git_config(rest)
    if sub == "reset":
        for t in rest:
            if t == "--":
                break
            if len(t) >= 4 and "--hard".startswith(t):     # --ha / --har / --hard
                return _f("git-hard-reset")
        return None
    if sub in _NOVERIFY_SUBS:
        f = _git_noverify(sub, rest)
        if f is None and sub == "push":
            f = _git_push_force(rest)
        return f
    return None


# -- everything else ---------------------------------------------------------------------------
def _h_rm(args: "list[str]") -> "Optional[Finding]":
    rec = force = False
    targets: "list[str]" = []
    opts = True
    for a in args:
        if opts and a == "--":
            opts = False
        elif opts and len(a) > 1 and a[0] == "-":
            if a[1] == "-":
                if len(a) >= 3 and "--recursive".startswith(a):
                    rec = True
                elif len(a) >= 3 and "--force".startswith(a):
                    force = True
            else:
                if "r" in a or "R" in a:
                    rec = True
                if "f" in a:
                    force = True
        else:
            targets.append(a)
    if rec and force:
        for t in targets:
            if _ROOTISH.fullmatch(t):
                return _f("rm-root-home")
    return None


def _h_chmod(args: "list[str]") -> "Optional[Finding]":
    rec = False
    pos: "list[str]" = []
    opts = True
    for a in args:
        if opts and a == "--":
            opts = False
        elif opts and len(a) > 1 and a[0] == "-":
            if a[1] == "-":
                if len(a) >= 3 and "--recursive".startswith(a):
                    rec = True
            elif "R" in a:
                rec = True
        else:
            pos.append(a)
    if rec and any(_ROOT_MODE.search(p) for p in pos) and any(_ROOTISH.fullmatch(p) for p in pos):
        return _f("chmod-root")
    if any(_SSH_PATH.search(p) for p in pos):
        for p in pos:
            if "/" in p or _SSH_PATH.search(p):
                continue
            if _OPEN_OCTAL.fullmatch(p) or _OPEN_SYMBOLIC.search(p):
                return _f("ssh-dir-chmod-open")
    return None


def _h_ssh_verb(args: "list[str]") -> "Optional[Finding]":
    for a in args:
        if _SSH_PATH.search(a):
            return _f("ssh-dir-mutation")
    return None


def _h_sed(args: "list[str]") -> "Optional[Finding]":
    for a in args:
        if a.startswith("--in-place") or (len(a) > 1 and a[0] == "-" and a[1] != "-" and "i" in a):
            return _h_ssh_verb(args)
    return None


def _h_docker(args: "list[str]") -> "Optional[Finding]":
    pos: "list[str]" = []
    i, n = 0, len(args)
    while i < n and len(pos) < 2:
        a = args[i]
        if len(a) > 1 and a[0] == "-":
            i += 2 if a in _DOCKER_VALUE else 1
        else:
            pos.append(a)
            i += 1
    return _f("docker-system-prune") if pos == ["system", "prune"] else None


def _h_dd(args: "list[str]") -> "Optional[Finding]":
    for a in args:
        if _DD_OF.match(a):
            return _f("dd-block-device")
    return None


def _h_declare(args: "list[str]") -> "Optional[Finding]":
    for a in args:
        if _ASSIGN.match(a):
            f = _check_assignment(a)
            if f:
                return f
    return None


# -- wrappers: `<wrapper> [options] cmd args` -> the command behind them ------------------------
def _w_generic(value_opts: "frozenset[str]" = frozenset(), npos: int = 0):
    def fn(words, i, ctx):
        j, n = i + 1, len(words)
        while j < n:
            t = words[j]
            if t == "--":
                j += 1
                break
            if len(t) > 1 and t[0] == "-":
                j += 2 if t in value_opts else 1
            else:
                break
        j += npos
        return j if j < n else None
    return fn


def _w_command(words, i, ctx):
    j, n = i + 1, len(words)
    while j < n and len(words[j]) > 1 and words[j][0] == "-":
        t = words[j]
        j += 1
        if t == "--":
            break
        if "v" in t or "V" in t:                # `command -v x` only looks a name up
            return None
    return j if j < n else None


def _w_env(words, i, ctx):
    j, n = i + 1, len(words)
    while j < n:
        t = words[j]
        if t == "--":
            j += 1
            break
        if t in ("-u", "--unset", "-C", "--chdir"):
            j += 2
        elif t in ("-S", "--split-string"):
            return ctx.program(" ".join(words[j + 1:])) if j + 1 < n else None
        elif t.startswith("--split-string=") or (t.startswith("-S") and not t.startswith("--")):
            first = t.partition("=")[2] if t.startswith("--") else t[2:]
            return ctx.program(" ".join([first] + words[j + 1:]))
        elif len(t) > 1 and t[0] == "-":
            j += 1
        else:
            break               # NAME=value operands and the command: _check_words reads them
    return j if j < n else None


_SUDO_VALUE = frozenset({"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t", "-U", "-D", "-R",
                         "--user", "--group", "--host", "--prompt", "--close-from",
                         "--command-timeout", "--role", "--type", "--other-user", "--chdir",
                         "--chroot"})


def _w_sudo(words, i, ctx):
    j, n = i + 1, len(words)
    shell = False
    while j < n:
        t = words[j]
        if t == "--":
            j += 1
            break
        if len(t) > 1 and t[0] == "-":
            if t in ("--shell", "--login") or (t[1] != "-" and re.fullmatch(r"-[A-Za-z]*[si][A-Za-z]*", t)
                                               and t not in _SUDO_VALUE):
                shell = True
            j += 2 if t in _SUDO_VALUE else 1
        else:
            break
    if shell:
        return ctx.program(" ".join(words[j:])) if j < n else None
    return j if j < n else None


_WRAPPERS = {
    "nohup": _w_generic(),
    "setsid": _w_generic(),
    "builtin": _w_generic(),
    "unbuffer": _w_generic(),
    "nice": _w_generic(frozenset({"-n", "--adjustment"})),
    "ionice": _w_generic(frozenset({"-c", "-n", "-p", "-P", "-u"})),
    "stdbuf": _w_generic(frozenset({"-i", "-o", "-e"})),
    "time": _w_generic(frozenset({"-f", "-o", "--format", "--output"})),
    "exec": _w_generic(frozenset({"-a"})),
    "timeout": _w_generic(frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "xargs": _w_generic(frozenset({"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--max-args",
                                   "--max-procs", "--max-lines", "--max-chars", "--delimiter",
                                   "--arg-file", "--eof"})),
    "command": _w_command,
    "env": _w_env,
    "sudo": _w_sudo,
    "doas": _w_generic(frozenset({"-u", "-C"})),
}


# -- interpreters: the text they are given is a program ----------------------------------------
_PIPE_HOPS = 8
_ECHO_FLAG = re.compile(r"-[neE]+")
_PRINT_ESC = re.compile(r"\\([nt\\])")


def _pipe_sources(cmd: "Optional[_Cmd]", ctx: "_Ctx") -> "list[str]":
    """Text that reaches a shell's stdin: its own heredocs / here-strings, and what the previous
    pipeline stage prints (a heredoc, a here-string, or the arguments of echo / printf)."""
    srcs: "list[str]" = []
    stage = cmd
    for hop in range(_PIPE_HOPS):            # `printf ... | tee f | bash`: walk back through the pipe
        if stage is None:
            break
        for r in stage.redirs:
            if r[0] == "<<<" and r[1] is not None:
                srcs.append(r[1])
            elif (r[0] == "<<" or r[0] == "<<-") and r[2] is not None:
                srcs.append(r[2])
        if hop:
            w = _strip_prefix(stage.words)
            if w and _cmd_name(w[0]) in ("echo", "printf"):
                args = w[1:]
                while args and _ECHO_FLAG.fullmatch(args[0]):
                    args = args[1:]
                if args and _cmd_name(w[0]) == "printf" and "%" in args[0]:
                    args = args[1:]
                # printf (and echo -e) turn \n and \t into line breaks: the shell sees them
                srcs.append(_PRINT_ESC.sub(lambda m: "\n" if m.group(1) == "n" else
                                           "\t" if m.group(1) == "t" else m.group(1),
                                           "\n".join(args)))
        stage = stage.pipe_from
    return srcs


def _strip_prefix(words: "list[str]") -> "list[str]":
    """Words without leading assignments (a cheap unwrap, enough to read echo / printf)."""
    i = 0
    while i < len(words) and _ASSIGN.match(words[i]):
        i += 1
    return words[i:]


def _h_shell(args: "list[str]", cmd: "Optional[_Cmd]", ctx: "_Ctx"):
    i, n = 0, len(args)
    code = stdin = False
    while i < n:
        t = args[i]
        if t == "--" or t == "-":
            i += 1
            break
        if len(t) < 2 or t[0] not in "-+":
            break
        if t[:2] == "--":
            i += 2 if t in ("--rcfile", "--init-file") else 1
            continue
        j = i + 1
        for ch in t[1:]:
            if ch == "c":
                code = True
            elif ch == "s":
                stdin = True
            elif ch == "o" or ch == "O":
                j += 1                              # each o/O takes the next word
        i = j
    if code:
        return ctx.program(args[i]) if i < n and args[i] else None
    if i >= n or stdin:                             # no script file: it reads stdin
        for src in _pipe_sources(cmd, ctx):
            f = ctx.program(src)
            if f:
                return f
    return None


def _h_eval(args, cmd, ctx):
    if args and args[0] == "--":
        args = args[1:]
    return ctx.program(" ".join(args)) if args else None


_SSH_VALUE_CHARS = frozenset("bcDEeFIiJLlmOopQRSWw")


def _h_ssh(args, cmd, ctx):
    i, n = 0, len(args)
    while i < n:
        t = args[i]
        if t == "--":
            i += 1
            break
        if len(t) > 1 and t[0] == "-":
            body = t[1:]
            for k, ch in enumerate(body):
                if ch in _SSH_VALUE_CHARS:
                    if k == len(body) - 1:
                        i += 1
                    break
            i += 1
            continue
        break
    # args[i] is the host; everything after it is the remote command line
    if i + 1 < n:
        return ctx.program(" ".join(args[i + 1:]))
    return None


def _h_su(args, cmd, ctx):
    n = len(args)
    for i, a in enumerate(args):
        if a in ("-c", "--command") or (len(a) > 1 and a[0] == "-" and a[1] != "-"
                                        and re.fullmatch(r"-[A-Za-z]*c", a)):
            return ctx.program(args[i + 1]) if i + 1 < n else None
        if a.startswith("--command="):
            return ctx.program(a[10:])
    return None


def _h_runuser(args, cmd, ctx):
    f = _h_su(args, cmd, ctx)
    if f:
        return f
    for i, a in enumerate(args):
        if a in ("-u", "--user") and i + 2 <= len(args):
            rest = args[i + 2:]
            if rest and rest[0] == "--":
                rest = rest[1:]
            return _check_words(rest, None, ctx) if rest else None
    return None


def _h_find(args, cmd, ctx):
    i, n = 0, len(args)
    while i < n:
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j, sub = i + 1, []
            while j < n and args[j] not in (";", "+"):
                sub.append(args[j])
                j += 1
            if sub:
                f = _check_words(sub, None, ctx)
                if f:
                    return f
            i = j
        i += 1
    return None


def _simple(fn):
    return lambda args, cmd, ctx: fn(args)


_HANDLERS = {
    "git": _simple(_h_git),
    "rm": lambda a, c, x: _h_rm(a) or _h_ssh_verb(a),
    "chmod": _simple(_h_chmod),
    "docker": _simple(_h_docker),
    "dd": _simple(_h_dd),
    "sed": _simple(_h_sed),
    "export": _simple(_h_declare), "declare": _simple(_h_declare), "typeset": _simple(_h_declare),
    "readonly": _simple(_h_declare), "local": _simple(_h_declare),
    "eval": _h_eval, "ssh": _h_ssh, "su": _h_su, "runuser": _h_runuser, "find": _h_find,
}
for _sh in _SHELLS:
    _HANDLERS[_sh] = _h_shell
for _verb in _SSH_VERBS - {"rm"}:             # rm has its own handler (which also runs this check)
    _HANDLERS[_verb] = _simple(_h_ssh_verb)
_KNOWN_NAMES = frozenset(_HANDLERS) | frozenset(_WRAPPERS)


class _Ctx:
    """Shared state of one classification: the work budget and the program nesting depth."""
    __slots__ = ("bud", "depth")

    def __init__(self, bud: _Budget, depth: int) -> None:
        self.bud = bud
        self.depth = depth

    def program(self, text: str) -> "Optional[Finding]":
        """Parse `text` as a shell program (a `bash -c` payload, a heredoc fed to a shell, ...)
        and classify every command in it."""
        if self.depth >= MAX_DEPTH:
            raise _Unparseable("nesting too deep")
        self.bud.spend(len(text) + 1)
        out: "list[_Cmd]" = []
        lx = _Lexer(text, out, self.bud, self.depth)
        lx.run(0, False)
        f = _forkbomb(text, lx.spans)
        if f:
            return f
        child = _Ctx(self.bud, self.depth + 1)
        for cmd in out:
            f = _check_cmd(cmd, child)
            if f:
                return f
        return None


def _check_cmd(cmd: _Cmd, ctx: _Ctx) -> "Optional[Finding]":
    if cmd.redirs:
        for r in cmd.redirs:
            if r[0] in _WRITE_REDIRS and r[1] and _SSH_PATH.search(r[1]):
                return _f("ssh-dir-mutation")
    words = cmd.words
    if not words:
        return None
    w = words[0]
    if "=" not in w:                       # not an assignment: skip names no rule looks at
        name = w.rpartition("/")[2]
        if name not in _KNOWN_NAMES and name[:4] != "mkfs":
            return None
    return _check_words(words, cmd, ctx)


def _check_words(words: "list[str]", cmd: "Optional[_Cmd]", ctx: _Ctx) -> "Optional[Finding]":
    i, n = 0, len(words)
    hops = 0
    while i < n:
        w = words[i]
        if "=" in w and _ASSIGN.match(w):
            f = _check_assignment(w)
            if f:
                return f
            i += 1
            continue
        name = _cmd_name(w)
        wrap = _WRAPPERS.get(name)
        if wrap is None:
            break
        hops += 1
        if hops > 16:
            return None
        nxt = wrap(words, i, ctx)
        if nxt is None:
            return None
        if isinstance(nxt, Finding):
            return nxt
        i = nxt
    else:
        return None
    name = _cmd_name(words[i])
    args = words[i + 1:]
    h = _HANDLERS.get(name)
    if h is not None:
        f = h(args, cmd, ctx)
        if f:
            return f
    if name.startswith("mkfs") and _MKFS.fullmatch(name):
        return _f("mkfs")
    return None


# ───────────────────────────────── coarse fallback scan ────────────────────────────────────────
_CRUDE_SEP = re.compile(r"[;&|\n()`]+")


def _clean(t: str) -> str:
    t = t.strip("\"'`")
    if t.startswith("$("):
        t = t[2:]
    return t.lstrip("({").rstrip(")};")


def _crude(command: str) -> "Optional[Finding]":
    """Fallback for input the lexer could not or may not process: keyword windows, no quoting,
    no heredocs.  Linear: split once, then `_CRUDE_WINDOW` tokens after each of the first
    `_CRUDE_TRIGGERS` trigger words and `_CRUDE_TAIL_WINDOW` after every later one."""
    f = _forkbomb(command, [])
    if f:
        return f
    triggers = 0
    for seg in _CRUDE_SEP.split(command):
        if not seg:
            continue
        toks = [_clean(t) for t in seg.split()]
        for i, t in enumerate(toks):
            if not t:
                continue
            if "=" in t and _ASSIGN.match(t):
                f = _check_assignment(t)
                if f:
                    return f
            sm = _SSH_PATH.search(t)
            if sm is not None:
                pre = t[:sm.start()]
                prev = toks[i - 1] if i else ""
                if (">" in pre and not pre.strip("0123456789&>|")) or \
                        (">" in prev and not prev.strip("0123456789&>|")):
                    return _f("ssh-dir-mutation")
            name = _cmd_name(t)
            h = _HANDLERS.get(name)
            if h is None and not (name.startswith("mkfs") and _MKFS.fullmatch(name)):
                continue
            if name in _SHELLS or name in ("eval", "ssh", "su", "runuser", "find"):
                continue                     # interpreters need real quoting; not in the coarse scan
            triggers += 1
            if name.startswith("mkfs"):
                return _f("mkfs")
            # past the cap only the next few words are read (every deny shape is decided within
            # a handful of them), so the cost stays bounded and nothing is skipped
            width = _CRUDE_WINDOW if triggers <= _CRUDE_TRIGGERS else _CRUDE_TAIL_WINDOW
            args = toks[i + 1:i + 1 + width]
            f = h(args, None, None)
            if f:
                return f
    return None


# ───────────────────────────────────────── public API ──────────────────────────────────────────
def classify_command(command: str) -> "Optional[Finding]":
    """The deny verdict for one Bash command: a ``Finding(rule, reason)`` or None. Never raises."""
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        if len(command) > MAX_CHARS:
            return _crude(command)
        try:
            return _Ctx(_Budget(WORK_FACTOR * len(command) + 20_000), 0).program(command)
        except (_Unparseable, RecursionError):
            return _crude(command)
    except Exception:
        try:
            return _crude(command) if isinstance(command, str) else None
        except Exception:
            return None
