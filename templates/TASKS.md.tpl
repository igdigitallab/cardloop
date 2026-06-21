# Tasks — {{name}}

Live project board. This file is the only one that sessions read.
Completed work goes to `DONE.md` (sessions do NOT read it — context hygiene).

Card format: `- [ ] text <!--ops:ID-->` inside a column section.
The `ops:ID` marker is added automatically — do not remove it. Numbered lists / nested lists / tables inside sections are NOT supported (cards get lost).

## Backlog
- [ ] Define project goal in CLAUDE.md (2-3 sentences: what and why)
- [ ] Set up memory (.claude-ops/memory/) — first entry: project context
{{#if_software_ops}}- [ ] Configure log_cmd and global error handler (cockpit visibility)
  > Set log_cmd in topics.json (how cockpit reads logs — journalctl / docker logs / tail). Add global error handler per project type (FastAPI / aiohttp / PTB / CLI — see CLAUDE.md ## Error Handler): handler must log full traceback and the line UNHANDLED exc_class=... path=.... Fill in ## ClaudeOps Integration Status in CLAUDE.md: mark error handler and log_cmd as "yes: where" instead of "no".
- [ ] Configure test_cmd in topics.json for automated quality checks
{{/if_software_ops}}{{#if_content}}- [ ] Organize source materials and outline
- [ ] Draft first section or article
{{/if_content}}{{#if_scratchpad}}- [ ] Capture initial notes and ideas
{{/if_scratchpad}}
## In Progress

## Review

## Failed
