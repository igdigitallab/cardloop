# spec-038 — Inline chat media (agent → cockpit screenshots, full-screen)

## Problem
The agent can only deliver screenshots/images to the operator via `tg-reply` (Telegram). There is no way to push an image **into the cockpit chat itself**. The operator wants the agent to send a screenshot directly into the project chat and open it full-screen (lightbox).

## Constraints / current state (verified)
- Frontend chat already renders agent text with `react-markdown` + `remark-gfm` (`web/src/tabs/ChatTab.tsx:1499-1500`). A markdown `![alt](url)` would render an `<img>` — but no route serves agent files, and there is no full-screen viewer.
- Backend streams turns over SSE (`webapp.py:7220-7304`). All `/api/*` routes are guarded by the `cops_auth` cookie middleware (`webapp.py:541-560`); exempt: `/api/health`, `/api/login`, project incident.
- Existing upload endpoint writes to `DATA/inbox/` (`webapp.py:5537-5580`) — a model for file handling, but it returns a filesystem path, not a served URL.
- `tg-reply` (`/usr/local/bin/tg-reply`) is the existing agent→operator file helper; the bot injects `TG_CHAT_ID`/`TG_THREAD_ID` env into the agent process. We mirror this pattern for the cockpit.

## Design

### Storage
- New gitignored dir `data/chat-media/<project_id>/`. Files named `<unix_ts>_<sanitized_name>.<ext>`.
- Add `data/chat-media/` to `.gitignore` (data/ already ignored — confirm coverage).

### Backend route (auth-guarded)
- `GET /api/projects/{id}/media/{filename}` → `web.FileResponse` of `data/chat-media/<id>/<filename>`.
  - Resolve project by id; 404 if unknown.
  - **Path-traversal guard:** reject filenames containing `/`, `..`, or that don't resolve inside the project media dir.
  - Content-Type from extension (png/jpg/jpeg/webp/gif). Sits under `/api/projects/...` so it inherits the cookie middleware automatically.
- Register near the existing project routes in `webapp.py` (next to `/upload`).

### Agent env
- When `run_engine`/`api_project_chat` launches the agent for a project, export `COPS_PROJECT_ID=<project_id>` and `COPS_MEDIA_DIR=<abs path to data/chat-media/<project_id>>` into the subprocess env (mirror of how the TG channel sets `TG_CHAT_ID`). This lets the helper resolve where to copy and which URL to print **without guessing**.

### CLI helper `cockpit-img`
- New script `/usr/local/bin/cockpit-img <path-to-image> [caption]` (installed alongside `tg-reply`; source kept in repo under `tools/cockpit-img` so it ships with the project).
- Behavior:
  1. Validate file exists and is an image; enforce a sane size cap.
  2. Require `COPS_MEDIA_DIR` + `COPS_PROJECT_ID` (error with a clear message if absent — "run from a cockpit project agent context").
  3. Copy file → `$COPS_MEDIA_DIR/<ts>_<name>.<ext>`.
  4. Print to stdout exactly one markdown line:
     `![<caption>](/api/projects/<COPS_PROJECT_ID>/media/<filename>)`
  - The agent includes that printed line in its reply text → it streams as a `text` event → react-markdown renders it inline.

### Frontend: inline render + full-screen lightbox
- In `ChatTab.tsx`, pass a custom `components={{ img: ChatImage }}` to the existing `ReactMarkdown`.
- `ChatImage`: renders `<img class="chat-msg-img" loading="lazy">` (max-width 100%, rounded, `cursor: zoom-in`, capped thumbnail height ~240px). On click → open a full-screen lightbox overlay.
- New `Lightbox` overlay component: fixed full-viewport, dark backdrop, the image centered/contained, click backdrop or ✕ or Esc to close. Themed via existing CSS vars. Mobile-friendly (pinch/scroll not required for v1; just contain-to-screen + tap-to-close).
- CSS in `chat.css`: `.chat-msg-img`, `.lightbox-overlay`, `.lightbox-img`, `.lightbox-close`.

## Out of scope (v1)
- Operator → agent image upload into chat (separate; `/upload` already exists for files).
- Pinch-zoom/pan inside the lightbox.
- Image deletion/retention policy (manual cleanup of `data/chat-media/` for now).

## Acceptance
- Agent in a project session runs `cockpit-img /tmp/shot.png "caption"`, includes the printed markdown in its reply → the cockpit chat shows the image inline; clicking opens it full-screen; Esc/tap closes.
- The media route 404s for unknown project and rejects path traversal.
- No image bleaks across projects (scoped by `<project_id>` dir).
- English-only code/UI; no hardcoded personal paths; `data/chat-media/` gitignored.
