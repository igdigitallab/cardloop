> Policy rules = what the agent may not (or should think twice before) do, written as Markdown files. Code map → ARCHITECTURE.md. Working rules → CLAUDE.md. HTTP → API.md.

# Policy rules

A rule is a `.md` file. The frontmatter says what to match, the body is the message the agent
reads. The cockpit evaluates every rule before **every tool call** of a Claude run (`Bash`,
`Write`, `Edit`, `mcp__*` — all of them) and either **blocks** the call or **warns** the agent
and lets it proceed. No code change, no restart: a rule file edited on disk applies to the very
next tool call, even in a chat that is already running.

Use it for policy a profession pack (or you) needs without touching code: "never send mail",
"warn before deleting under the client-deliverables folder", "no writes to `.env`".

**Scope: Claude engine only.** Rules ride on the Claude Agent SDK's `PreToolUse` hook. Codex and
Grok runs have no equivalent hook, so rules do not apply to them (v1). Ask mode and plan mode do
not change anything: rules apply in every permission mode. Sub-agents are covered too (checked
against the real CLI: the callback fires for a sub-agent's own tool calls and a block reaches it).

**A rule is a policy tripwire, not a sandbox.** It matches the tool call the model *makes*. A
model that wants the same effect through a command shape your pattern does not see (`python -c`,
a symlink, a different tool) is not stopped. Pair rules with the permission modes and with the
built-in dangerous-command guard; do not rely on a regex as the only wall around something that
must never happen.

**Rule files are ordinary files.** An agent with Bash or Write can edit or delete them, and a
project file with the same name as a global rule overrides it (including `enabled: false`). Where
a rule must hold against the agent itself, keep it in a directory the cockpit's user cannot write
(a pack dir owned by another user, mode `0555`) — but note a project file can still override it by
name: a non-overridable ("locked") tier is not part of v1.

## Where rule files live

| Tier | Directory | Notes |
|------|-----------|-------|
| `project` | `<project>/.claude-ops/rules/` | Highest precedence. Subject to the [trust rule](#the-trust-rule-git-tracked-files-are-off-by-default). |
| `global` | `$CARDLOOP_RULES_DIR`, else `~/.claude-ops/rules/` | Every project on this cockpit. |
| `pack` | directories handed to the loader (`extra_dirs`) | Lowest precedence. The extension point for profession packs: `policy_rules.make_hook(..., extra_dirs=[...])` or a callable returning the list. This module only loads them. |

Files are read non-recursively (`*.md`, no dotfiles). Same-named rules **override as a whole
file**, highest tier wins — so a project can replace a global rule, and a project file with
`enabled: false` under the same name switches that global rule off for the project. An *invalid*
file does not override anything (the lower tier stays in force). The identity of a rule is its
frontmatter `name`, falling back to the file name.

## Format

```markdown
---
name: my-rule
enabled: true            # optional, default true
event: mcp               # bash | file | mcp | all   (optional, default all)
action: block            # block | warn              (optional, default warn)
tool_matcher: ^mcp__mail__send$      # optional regex on the tool name
pattern: ...             # shortcut: one regex (see below), OR
conditions:              # all of them must match
  - field: tool_input.to
    operator: contains
    pattern: '@competitor.com'
---
The message the agent reads. Markdown is fine.
```

A rule needs at least one of `tool_matcher`, `pattern`, `conditions` (otherwise it would match
every call), and not both `pattern` and `conditions`. Unknown keys, duplicate keys and bad values
make the file invalid — a typo must not silently weaken a rule.

| Key | Values |
|-----|--------|
| `event` | `bash` = Bash/PowerShell; `file` = Write, Edit, MultiEdit, NotebookEdit; `mcp` = tools named `mcp__*`; `all` = every tool (use `tool_matcher` to narrow it, e.g. `^Read$` for reads) |
| `tool_matcher` | regex searched in the tool name — anchor it (`^(Write\|Edit)$`), `Write` alone also matches `NotebookWrite` |
| `pattern` | regex matched against the call's primary text: the command for Bash; file path and content for file tools; tool name plus the whole input as JSON for everything else |
| `conditions[].field` | `tool_name`, `command`, `file_path`, `content` (Write `content` / Edit `new_string` / NotebookEdit `new_source` — never the text an Edit removes), or `tool_input.<key>` (dotted path, list index allowed: `tool_input.to.0`; non-string values are matched as JSON) |
| `conditions[].operator` | `regex_match` (default), `contains`, `equals`, `not_contains`, `starts_with`, `ends_with` |
| `action` | `block`: the call is denied and the agent reads your message as the reason. `warn`: the call runs and the agent gets your message as extra context |

Semantics worth knowing:

- `regex_match` and `pattern` are case-insensitive and multi-line (`^`/`$` match per line).
  Switch case sensitivity back on inline: `(?-i:RM)`. The other operators are exact.
- A condition on a field the call does not have (a `command` condition on a `Write`) never
  matches — not even `not_contains`.
- A `MultiEdit` is checked edit by edit: all conditions must hold on the *same* edit.
- Values: write regexes unquoted or in single quotes (`pattern: 'rm\s+-rf'`). In double quotes
  YAML escapes apply, so a backslash must be doubled; an invalid escape makes the file invalid
  instead of silently changing the pattern. `pattern:` and `tool_matcher:` keep everything to
  the end of the line (` #` is part of the regex there); other keys accept a trailing `# comment`.

### Example 1 — block a tool outright

`~/.claude-ops/rules/block-mail-send.md`

```markdown
---
name: block-mail-send
event: mcp
action: block
tool_matcher: ^mcp__mail__send$
---
Sending mail is not allowed in this workspace. Draft the message in the chat and ask the
operator to send it.
```

Any call to `mcp__mail__send` is denied; the agent sees the message as the reason and tells
the operator instead of retrying.

### Example 2 — warn before deleting under a directory

`<project>/.claude-ops/rules/warn-rm-client-files.md`

```markdown
---
name: warn-rm-client-files
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: \brm\b
  - field: command
    operator: contains
    pattern: /srv/client-files
---
You are deleting under /srv/client-files, which holds delivered work. Confirm the exact path
with the operator first; prefer moving files to /srv/client-files/.trash.
```

The `rm` runs, but the agent reads the warning right before it does. Change `action: warn` to
`action: block` and the same file becomes a hard stop.

## What the agent and the SDK see

Both are the exact PreToolUse hook outputs, verified against the real CLI.

Block (several matching blockers are listed in one reason; blockers win over warnings):

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "Blocked by policy rule:\n[block-mail-send] Sending mail is not allowed ..."}}
```

Warn:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "additionalContext": "Policy warning (the call is allowed to proceed):\n[warn-rm-client-files] You are deleting ..."}}
```

A warning deliberately carries **no** `permissionDecision`. `"allow"` would skip the permission
prompt (measured: with `allow` the ask-mode gate is never consulted), so a warn rule would
silently auto-approve the call. No match, or no rules at all, returns `{}` (with no rule files the cost is one directory listing per tier).

Every match is written to the audit log as `RULE: <name> <action>: <input>` (200 characters
max, the command for Bash, `<tool> <path>` for file tools, `<tool> {argument names}` for the
rest — never file contents or argument values) and counted per rule in memory.

## The trust rule: git-tracked files are off by default

Opening a cloned repository must not silently install policy or inject text into the agent. So
a **project-tier rule file that git tracks** (committed, or merely staged) is **ignored** unless
the operator opted that project in:

```
POST /api/projects/{id}/settings   {"rules_trust_tracked": true}
```

(or the toggle in the project's Agents tab). Details:

- Untracked project files (the usual case: you wrote them locally, or they are in `.gitignore`)
  and every global/pack file load normally.
- An untrusted file is not even read, and it does **not** take part in the override resolution —
  otherwise a repo could ship `enabled: false` under the name of your global rule and switch
  it off. It still shows up in the list as `untrusted`, with the reason.
- If git cannot answer (not installed, times out, refuses the repository, e.g. a different
  owner), the project tier is treated as tracked: disabled. A directory outside any git
  work tree has nothing tracked and loads normally.
- The answer is refreshed every 10 seconds and whenever a rule file changes. The opt-in is read
  live on every tool call.
- A symlinked rules directory or rule file in the project tier is refused (a tracked symlink
  could smuggle in a file git does not list).

## Limits

| Limit | Value | Behaviour when exceeded |
|-------|-------|-------------------------|
| File size | 64 KB | file skipped, diagnostic |
| Rules loaded | 100 (project tier fills the slots first) | extra files skipped, diagnostic |
| Conditions per rule | 16 | file invalid |
| Pattern length (regex or condition) | 512 characters | file invalid |
| Message | 8000 characters | truncated |
| Nested quantifiers `(a+)+`, `(.*)*`, `(\w+\s*)*`; backreferences; ambiguous alternation under a quantifier `(a\|aa)+` | rejected statically | file invalid |
| One regex call sees at most 64 KB | longer input is scanned in overlapping 64 KB windows, up to 1 MiB | see below |
| Regex wall clock | 0.1 s per rule, 0.5 s per tool call | see below |

A bad file is skipped with a diagnostic (logged once per change, shown in the Agents tab and by
the API) and never breaks a turn or hides the other rules.

**Python's `re` cannot be given a timeout**, and the usual unanchored shapes (`\s+x`, `.*a.*b`,
`(ab)*c`) are quadratic on long input — measured at minutes for 64 KB. The static check removes
only the exponential shapes. So regex work runs under a `SIGALRM` watchdog (needs the main
thread, which is where the cockpit's event loop runs; elsewhere the windows shrink to 4 KB).
Anything that could not be decided — timeout, input beyond 1 MiB, evaluation budget spent — is
*unknown*: a `block` rule **fails closed** (the call is denied, with a note that the input could
not be inspected), a `warn` rule stays silent. Write patterns that start with a literal
(`rm\s+-rf`, not `\s+-rf`) and prefer `contains` / `starts_with` / `ends_with` for plain text:
they are linear and see the whole input.

## Seeing what fired

- **Agents tab → Policy rules**: every file with tier, status (`active`, `disabled`, `untrusted`,
  `shadowed`, `invalid`), action, event, hit count since the cockpit started, and diagnostics.
  Read-only; edit the file in the Files tab.
- `GET /api/projects/{id}/rules` — the same as JSON (see API.md).
- Audit log (`data/audit/`): `RULE: no-rm block: rm -rf /srv`.
- Hit counters live in memory and reset when the cockpit restarts; the audit log is the history.

## Not covered (v1)

- Codex and Grok runs (no SDK hook). Only the Claude engine.
- The UI is read-only; no rule editor, no test-a-rule button.
- Events other than `PreToolUse`: no prompt, stop or post-tool rules.
- A rule sees the tool call, not its effect: it cannot tell what a shell command will really do.
