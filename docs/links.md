# Links, not paths

Bosire reads all of this on a phone, in Telegram. A message ending
`2026-08-28-brefo-wedding-taxi/train-route.md` is a dead end there — there is no
filesystem to open it from. GitHub renders markdown natively and he is signed
in, so a blob URL just works.

So: **every file a job or the concierge points him at goes out as a GitHub URL.**

## The ordering, which is the whole problem

A link to a file that has not been pushed yet is worse than the path it
replaced: he taps it and gets a 404, and now he has neither the file nor the
path. So the push has to have already happened when the message arrives.

It is tempting to leave that to the `Stop` hook — `~/.claude/hooks/claude-wip.sh`
runs `bootstrap wip`, which commits and pushes the repo after every assistant
turn. It cannot work. That hook is registered `async` and fires **when the turn
ends**; `notify` is a tool call **inside** the turn. The hook is not a race that
sometimes loses, it is a race that loses every single time, by construction.

`concierge notify --file` therefore does the push itself, synchronously, before
`telegram.send`:

    write the file  →  git add <that file>  →  git commit  →  git push  →  send

`concierge.publish.publish()` is that middle span. `tests/test_cli.py::
test_the_file_is_pushed_before_the_message_is_sent` pins the order.

## Deliberately narrow

`bootstrap wip` stages with `git add -A` and has no size guard; on 2026-08-16 it
swallowed a 437 MB EPUB and wedged the repo. Adding a second thing that commits
must not make that worse, so `publish()`:

- stages **exactly the one file being linked**, by path, and commits with
  `git commit -- <that path>` so a dirty tree around it is not swept in;
- refuses outright above `MAX_BYTES` (25 MB) and reports a path instead. What a
  job links is a markdown report for reading on a phone; anything larger is a
  mistake, and committing a mistake is what caused the incident;
- makes no empty commit when the file is already committed and pushed.

## Where the URL comes from

`owner/repo` is read from the repo itself — `git remote get-url origin`, parsed
for both the SSH (`git@github.com:Owner/repo.git`) and HTTPS forms. Not from
`concierge.toml`: that file lists the repos a job may be *spawned* in, which is
a different question, goes stale when a remote is renamed, and says nothing
about a repo nobody added. The configured `github =` value survives as the
fallback for a repo with no origin.

The ref is the branch the push actually landed on, not the default branch. On a
side branch, `main` would not contain the file.

## When it cannot link

Every failure degrades to the local path **with the reason attached**, and the
message still goes out — a message that does not arrive is worse than a message
with a path in it. That covers: no origin remote, a repo with no configured
GitHub url either, a file that does not exist, a file over the size ceiling, a
push that fails offline or on auth, and any unexpected exception from git.

## The two entry points

- `concierge notify "<text>" --file <name>` — a job's normal route. The file is
  relative to the job's task folder; the link is appended to the message.
- `concierge link <path>` — pushes one file and prints its URL. This is for the
  concierge, which answers short questions inline and so never goes through
  `notify` at all, and for a job that needs the URL *inside* its prose rather
  than appended to it.

## What this does not do

A link carries the detail; it does not replace the answer. If he has to act on
something, the thing to act on stays in the message and the link holds
everything behind it. Both prompts say so. A reply that is nothing but a URL is
a regression, not a feature.
