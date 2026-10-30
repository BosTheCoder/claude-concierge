# Claude button: start a Claude Code chat from any page

**Status:** draft for Kelvin's review, 30 Oct 2026.

## Why

Kelvin's pages show him things that need a Claude session: the morning brief says
"8 tool calls refused", an item is wrong, a run should go again. Today that means
opening a terminal or typing the whole situation into Telegram. He wants a button on
the page that starts a Claude Code chat already knowing what he was looking at, with
room for a note ("rerun it", "fix the refused tools", "this was paid, find out why it
missed it"), or no note at all. The brief is the first page; every future page on the
tailnet should get it with one line.

## What he sees

- A ✦ button where it makes sense on a page (each brief item; the brief's header).
- Tap: a small sheet with the thing's title, an optional note box and **Go**.
- Go: "Starting…" for up to ~20 s, then the chat opens in the Claude app
  (`https://claude.ai/code/<session>`). He watches it work and can talk to it.
- The job is an ordinary concierge job: it shows in `/jobs`, reports back on Telegram
  with `concierge notify`, and the reaper closes it when idle.
- If it fails (repo not allowed, spawn died) the sheet says why in one line.

## How it works

**One shared service: the concierge dashboard server** (127.0.0.1:8787, already kept
up by `ensure-up` every 5 min). It gains two routes:

- `GET /claude.js`: the button widget.
- `POST /api/spawn` `{title, cwd, context, note}` → `spawn.spawn_job(...)` →
  `{ok, id, rc_url}`.

**Tailscale Serve `/claude` → `http://127.0.0.1:8787`** on the main host. Serve strips
the prefix, so every app on `bos-desktop.fish-grouper.ts.net` reaches it
**same-origin**: no CORS, no per-app backend code. Adding the button to a page is:

```html
<script src="/claude/claude.js"></script>
<button onclick='claudeAsk({title: "…", cwd: "~/projects/personal/<repo>", context: () => "…"})'>✦</button>
```

The page draws its own ✦ buttons; `claudeAsk` opens the sheet. `context` can be a
function so it reads the page's state at tap time. `cwd` must be a repo listed in
`concierge.toml`.

**The brief the job gets:**

```
Started from a button on <page URL> by Kelvin.
His note: <note, or "none: work out what's needed from the context">
What he was looking at:
<context>
```

`chat_id` is the concierge's last chat (`registry.last_chat()`), so `notify` lands in
his usual Telegram chat. No task folder unless the job decides it needs one.

**Guards** (tailnet-only, like everything else here):

- `cwd` must be a repo in `concierge.toml` (the existing `resolve_cwd` check).
  `inbox-brief` gets added there.
- JSON `Content-Type` required and `Sec-Fetch-Site: cross-site` refused, same as the
  brief's own POSTs, so another site can't start sessions through his browser.
- `context` capped at 30,000 characters; title at 120.

Jobs run in the concierge's permission mode (bypass), and the job prompt already says
to stop and ask before anything outward or irreversible the brief didn't ask for.

## The brief (first user)

- ✦ on each item. Context: the item record (title, text, refs, links, why with its
  evidence and rule, history, feedback) and where that run's trace is
  (`state/traces/<date>.jsonl`, `state/runs/<date>.json`).
- ✦ in the header beside the refusals / last-run line. Context: last run time, cost,
  refused tools, trace and run file paths, and that `./run.sh` reruns it. Note box
  placeholder: "e.g. rerun it, or fix the refused tools".

## Not now

- A background mode with no chat (he chose chat).
- Showing job status on the page; Telegram and `/jobs` already do.
- Auth beyond the tailnet.

## Calls I made (check these)

- The service lives in the concierge, not the brief, so every future page reuses it.
- Reruns are the same button with a note, not a separate Rerun button.
- Jobs report to the concierge's last Telegram chat.
