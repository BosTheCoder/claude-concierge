# Remote Control

Three different things in this repo wear the name "remote control". They're
worth keeping straight, because two of them are supervised separately and the
third is a background sweep.

| | what it is | where |
|---|---|---|
| `--remote-control concierge` | Publishes the one long-lived concierge session so the phone can talk to **it** | `supervisor.py` |
| the sweep | Finds ordinary sessions that silently fell off the bridge and types `/rc` to put them back | `rc.py` |
| the server | Puts **this machine** in the Claude app's device list, so a session can be *started* from the phone | `rcserver.py` |

## The sweep

Claude Code sessions drop off Remote Control silently. Nothing in the session
says so; the link just stops working. The sweep runs on every `ensure-up` tick,
reads `~/.claude/sessions/*.json` for rows whose bridge is gone, and types `/rc`
into the corresponding tmux pane.

```bash
concierge sessions     # read-only: what's connected, what's fallen off
concierge rc           # the fix
```

Sessions outside tmux have no pane to type into, so those have to be
reconnected by running `/rc` in them yourself. `concierge sessions` tells you
which are which.

Two details keep the sweep from doing damage. It addresses panes by **pane id**,
not window index — indexes shift when a window closes, and the cost of typing
into the wrong Claude Code session is that it acts on it. And it captures panes
with escape sequences intact (`capture-pane -pe`), because Claude Code draws the
ghost of your last message into the empty input box in dim SGR-2. Without the
codes there's no way to tell that ghost from text genuinely waiting to be sent.

## The server

`claude remote-control` in server mode is what puts the machine itself in the
device list in the Claude app. Sessions are then created on demand from your
phone, in the default repo, up to the CLI's default capacity of 32.

```bash
concierge rc-server        # idempotent; safe every five minutes
```

Give it its own logon task and its own five-minute watchdog, in their own
namespace rather than riding `ensure-up`. A concierge that fails to start must
not also take this machine off the app, and a broken server must not stop the
concierge. Started by hand it works fine until the next reboot, which is exactly
the failure this repo exists to close.

### Health has to be read off the pane

Measured against a live server: the server process writes **no** row to
`~/.claude/sessions`, and the sessions it spawns declare `entrypoint: "sdk-cli"`
with the `bridgeSessionId` key *absent* rather than null. So the registry check
that `rc.py` relies on has nothing to read here.

(The same measurement is why the sweep is safe: `participates` is false for every
one of those rows, so it will never type `/rc` into a server pane.)

What the pane says, from the CLI's own renderer: `Connecting` while it comes up,
the session title once connected, and `Reconnecting · retrying in Xs ·
disconnected Ys` while its backoff runs.

Two things follow. The status is read **last match wins** — the startup banner
stays on screen for the life of the server, so a pane that's been up for a day
still has the word "Connecting" near the top, and reading top-down would report
a healthy server as permanently mid-connect.

And a single bad reading must not trigger a restart. The CLI's own backoff is
real and usually wins — a ten-minute network drop recovers on its own — so it
takes four consecutive non-`Connected` ticks, twenty minutes, before the window
is recycled. Recycling kills whatever you had open from the phone, so it must
not fire on a blip. When it does fire, it says so over Telegram.
