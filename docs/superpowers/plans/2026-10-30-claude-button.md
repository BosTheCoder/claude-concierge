# Claude button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A ✦ button on any tailnet page that starts a concierge job (live Claude chat) seeded with what the page shows.

**Architecture:** The concierge dashboard server gains `GET /claude.js` and `POST /api/spawn`; Tailscale Serve `/claude` → 127.0.0.1:8787 makes both same-origin for every app on the main host. The brief is the first page to use it.

**Tech Stack:** Python stdlib http.server (existing), vanilla JS, pytest (concierge), unittest (brief).

**Spec:** `docs/superpowers/specs/2026-10-30-claude-button-design.md`

## Global Constraints

- Paths served both bare and under `/claude` (Serve strips the prefix; the dashboard port serves it bare).
- Guards: JSON Content-Type else 400; `Sec-Fetch-Site: cross-site` → 403; cwd via `spawn.resolve_cwd`; title ≤ 120, context ≤ 30,000, note ≤ 4,000 chars.
- `chat_id` = `registry.last_chat()`; none → 503 with a one-line reason.
- Never spawn a real job from tests.

## Review Focus

- Spawn takes ~20 s: the sheet must say it's working and not allow a double Go.
- Popups are blocked after an await: the chat opens from a link he taps, not `window.open`.
- A job whose URL never appears still returns its id, and the sheet says where to find it.
- Any field missing or the wrong type → 400, never a 500.
- The sheet must fit a phone at 360 px with the keyboard up.

---

### Task 1: spawn endpoint + widget (concierge)

**Files:** Create `concierge/claude.js`. Modify `concierge/dashboard.py` (routes, `job_brief`). Test `tests/test_dashboard.py`.

**Interfaces:**
- Produces: `job_brief(page: str, note: str, context: str) -> str`; `window.claudeAsk({title, cwd, context, placeholder?})`.
- `POST /api/spawn {title, cwd, context, note, page}` → 200 `{ok, id, rc_url}` | 4xx/5xx `{ok: false, error}`.

- [ ] Test: POST with a stubbed `spawn_job` returns its id and url and passes a brief containing the note and context; wrong Content-Type → 400; cross-site → 403; cwd refused → 400; bare and `/claude` paths both route.
- [ ] Implement; run `just test` (or `uv run pytest`).
- [ ] Commit.

### Task 2: the brief uses it

**Files:** Modify `site/index.html` (inbox-brief). Local config: `concierge.toml` gets the inbox-brief repo.

- [ ] ✦ in each card's and line's actions; ✦ in the header. Context built from the item record or `data.meta`, plus trace and run file paths.
- [ ] `tests/page_check.js` still passes; screenshot at 360 px with the sheet open.
- [ ] Commit.

### Task 3: deploy

- [ ] `tailscale.exe serve --bg --set-path /claude http://127.0.0.1:8787`; restart the dashboard window.
- [ ] Start one real job from the brief page; confirm it appears in `concierge jobs` and its link opens.
