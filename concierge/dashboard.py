"""A page showing every Claude process on the box, and a button to close the
idle ones.

WHY A SERVER AND NOT A COMMAND
------------------------------
`bin/concierge reap --dry-run` already prints most of this, and is useless for
the case that prompted it: Bosire on his phone, no terminal open, wanting to
know what is holding the machine's memory and to get some of it back. So this
is a page, it is always up, and it is reachable over his tailnet.

WHY THE SAMPLER
---------------
"Is it still working?" is answered mostly by a CPU rate, and a rate needs two
readings separated by time. A request cannot wait 30 seconds for one, so a
background thread takes a reading every few seconds and every request reads the
answer it has already computed. Before the first pair exists, rows say
`unknown` and offer no button — which is the correct thing to show for the
first half-minute after a restart.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No authentication. The bind address is the access control: loopback by default,
and `--expose` publishes it on the Tailscale address and nowhere else, which is
the same trust boundary the vault browser already runs on (`browser-vnc`,
ports 6080/6081). Anything that can reach this page is already a device he has
signed into his own tailnet. If that stops being true, this is the thing to fix
first — it can kill processes.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from concierge import config, sessions, settings as settings_mod, tmuxctl

# Fast enough that a reap shows up on the next poll, slow enough that the
# dashboard is not itself a load on the machine it exists to relieve. One poll
# is a /proc scan plus one `tmux capture-pane` per pane — milliseconds.
SAMPLE_SECONDS = 5.0
# A rate over less than this is noise: an idle REPL's polling loop is bursty at
# second resolution, and a burst read as 3 ticks/s would show as "running".
MIN_WINDOW_SECONDS = 20.0
# And a rate is measured over the SHORTEST honest window, not the longest.
# Measured 2026-09-04: a session that had just finished a 33-second turn still
# averaged 7.3 ticks/s over a 150-second window and read as running for two
# minutes after it stopped. Taking the newest reading that is at least
# MIN_WINDOW old instead means a finished session settles to idle in about
# half a minute. MAX is only a sanity bound now — a gap larger than this means
# the sampler stalled and there is no rate worth reporting.
MAX_WINDOW_SECONDS = 150.0

DEFAULT_PORT = config.DASHBOARD_PORT
# Published by Docker Desktop, which runs on the Windows side and so can bind
# the host's Tailscale address; a listener inside WSL cannot. See `expose`.
PROXY_CONTAINER = "claude-dash-proxy"
TAILSCALE_EXE = Path("/mnt/c/Program Files/Tailscale/tailscale.exe")


class Sampler:
    """Rolling CPU readings for every Claude process, one thread, no locks held
    across a syscall."""

    def __init__(self, interval: float = SAMPLE_SECONDS) -> None:
        self.interval = interval
        self._history: dict[int, list[tuple[float, int]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def sample(self, *, now: float | None = None) -> None:
        now = now or time.time()
        seen = set()
        for proc in sessions.scan().values():
            if not sessions.is_claude(proc):
                continue
            seen.add(proc.pid)
            with self._lock:
                series = self._history.setdefault(proc.pid, [])
                if series and proc.ticks < series[-1][1]:
                    # The counter went backwards, so the pid was recycled under
                    # us. Throw the history away rather than reporting a rate
                    # computed across two different processes.
                    series.clear()
                series.append((now, proc.ticks))
                cutoff = now - MAX_WINDOW_SECONDS - self.interval
                while len(series) > 2 and series[0][0] < cutoff:
                    series.pop(0)
        with self._lock:
            for pid in set(self._history) - seen:
                del self._history[pid]

    def rates(self, *, now: float | None = None) -> dict[int, float]:
        """Ticks per second per pid, omitting any pid we cannot answer for.

        Omission is the point: `sessions.classify` turns a missing rate into
        `unknown`, which shows no reap button. Reporting a bad number instead
        would show one.
        """
        now = now or time.time()
        out: dict[int, float] = {}
        with self._lock:
            history = {pid: list(series) for pid, series in self._history.items()}
        for pid, series in history.items():
            if len(series) < 2:
                continue
            newest_at, newest = series[-1]
            # The NEWEST reading old enough to give a smooth rate. Going
            # further back only drags a finished turn's CPU burst into the
            # average and keeps the session showing as running.
            baseline = None
            for at, ticks in reversed(series):
                if MIN_WINDOW_SECONDS <= newest_at - at <= MAX_WINDOW_SECONDS:
                    baseline = (at, ticks)
                    break
            if baseline is None:
                continue
            elapsed = newest_at - baseline[0]
            if elapsed <= 0 or newest < baseline[1]:
                continue
            if now - newest_at > MAX_WINDOW_SECONDS:
                continue  # the sampler thread has stopped or stalled
            out[pid] = (newest - baseline[1]) / elapsed
        return out

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the thread
                pass
            self._stop.wait(self.interval)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True, name="sampler")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def handler_for(sampler: Sampler):
    class Handler(BaseHTTPRequestHandler):
        server_version = "concierge-dashboard"

        def log_message(self, *_args) -> None:
            """Silence. This runs in a tmux window nobody watches, and one line
            per poll every five seconds forever is not a log, it is a leak."""

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if self.path.split("?")[0] == "/api/sessions":
                self._json(200, sessions.snapshot(sampler.rates()))
                return
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                return
            if self.path == "/manifest.webmanifest":
                self._send(200, MANIFEST.encode(), "application/manifest+json")
                return
            if self.path == "/icon-512.png":
                self._send(200, ICON.read_bytes(), "image/png")
                return
            self._send(404, b"no", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/reap":
                self._send(404, b"no", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or "{}")
                pid = int(body["pid"])
                fingerprint = int(body["fingerprint"])
            except (ValueError, KeyError, TypeError):
                self._json(400, {"ok": False, "error": "need pid and fingerprint"})
                return
            try:
                message = sessions.reap(pid, fingerprint, rates=sampler.rates())
            except sessions.RefusedError as exc:
                self._json(409, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - report, never 500 silently
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(200, {"ok": True, "message": message})

    return Handler


# --- reaching it from the phone ----------------------------------------------


def tailnet_ip() -> str | None:
    """The Tailscale address, read from Windows rather than assumed.

    Tailscale runs on the Windows host, not inside WSL — `tailscale` is not on
    PATH here and never will be. Same source `browser-vnc/env.sh` reads, for
    the same reason: the bind address IS the access control, so a stale
    hard-coded value is a security question rather than a convenience one.
    """
    if not TAILSCALE_EXE.exists():
        return None
    try:
        out = subprocess.run(
            [str(TAILSCALE_EXE), "ip", "-4"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        candidate = line.strip().strip("\r")
        if candidate.startswith("100."):
            return candidate
    return None


def expose(port: int, *, runner=subprocess.run) -> str:
    """Publish the dashboard on the tailnet, and say where.

    THE PROBLEM. This machine is WSL2 in NAT mode. A listener here is reachable
    on the Windows host's 127.0.0.1 (that is what `localhostForwarding` does)
    and on nothing else — measured 2026-09-04: Windows could reach
    127.0.0.1:8787 and could not reach 100.79.135.108:8787. So binding
    0.0.0.0 inside WSL does not put the page on his phone, and nothing about
    the Python server can change that.

    THE ROUTE. Docker Desktop runs on the Windows side, so a published port is
    bound by Windows and CAN take the tailnet address. A container publishing
    `<tailnet-ip>:8787` and forwarding to `host.docker.internal:8787` lands
    back on the Windows loopback, which forwards into WSL. Four hops, all of
    them already in place for other reasons — this adds no new moving part to
    the machine, which is why it beats a `netsh portproxy` that needs an
    Administrator shell and breaks every time WSL's IP changes on reboot.

    Bound to the tailnet address specifically, never 0.0.0.0: on the LAN or a
    coffee-shop network this page would be an unauthenticated kill switch.
    """
    ip = tailnet_ip()
    if not ip:
        return "no Tailscale address — the page is on localhost only"
    # Every docker call here is bounded. This runs from `ensure-up`, which is
    # the watchdog that keeps the concierge alive; a docker daemon that is
    # starting, stopped or wedged must cost a log line, never a hung tick.
    try:
        runner(
            ["docker", "rm", "-f", PROXY_CONTAINER],
            capture_output=True, text=True, timeout=30,
        )
        result = runner(
            [
                "docker", "run", "-d",
                "--name", PROXY_CONTAINER,
                # So it comes back with Docker Desktop after a reboot. The
                # WSL-side server may not be up yet when it does; socat simply
                # refuses connections until it is, and needs no restart itself.
                "--restart", "unless-stopped",
                "-p", f"{ip}:{port}:{port}",
                "alpine/socat",
                f"TCP-LISTEN:{port},fork,reuseaddr",
                f"TCP:host.docker.internal:{port}",
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"tailnet proxy not started: {exc}"
    if result.returncode != 0:
        return f"tailnet proxy failed: {(result.stderr or '').strip()}"
    return f"http://{ip}:{port}/"


def proxy_alive(*, runner=subprocess.run) -> bool:
    """Is the tailnet proxy container up? Cheap enough to ask every five
    minutes, and asking is what stops `ensure_up` restarting it every tick."""
    try:
        result = runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", PROXY_CONTAINER],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and (result.stdout or "").strip() == "true"


def responding(port: int = DEFAULT_PORT, *, timeout: float = 3.0) -> bool:
    """A window with a dead python in it is not a dashboard.

    tmux will happily report the window exists long after the process in it
    stopped serving, and that is the failure this check is for: a page that
    times out on his phone is worse than no page, because he assumes the
    machine is idle.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/sessions", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_up(*, tmux=tmuxctl, port: int = DEFAULT_PORT) -> str:
    """Keep the dashboard and its tailnet proxy up. Ridden by `ensure-up`.

    Its own window in the concierge tmux session rather than a scheduled task
    of its own: the session is already the thing this machine keeps alive, it
    is already spared by the standalone session-reaper (`PROTECTED_TMUX`), and
    a dashboard that needs its own supervision would be one more thing to rot.
    """
    if not tmux.has_session(config.TMUX_SESSION):
        return "dash: no concierge session yet"

    notes = []
    if not proxy_alive():
        notes.append(f"dash-proxy: {expose(port)}")

    if responding(port):
        return "; ".join(notes) if notes else "dash: healthy"

    # Not answering. Replace the window rather than adding a second one — a
    # stale window holding the port would keep the new one from binding it.
    if config.DASHBOARD_WINDOW in tmux.list_windows(config.TMUX_SESSION):
        tmux.kill_window(config.TMUX_SESSION, config.DASHBOARD_WINDOW)
    command = tmux.build_shell_command(
        str(settings_mod.REPO_ROOT),
        [
            str(settings_mod.REPO_ROOT / "bin" / "concierge"),
            "dash",
            "--port",
            str(port),
            "--no-expose",  # the proxy is handled above, on its own schedule
        ],
    )
    tmux.new_window(config.TMUX_SESSION, config.DASHBOARD_WINDOW, command)
    notes.append("dash: started")
    return "; ".join(notes)


def serve(
    port: int = DEFAULT_PORT,
    host: str = "0.0.0.0",  # noqa: S104 - WSL NAT; see expose() for why
    *,
    exposed: bool = True,
    echo=print,
) -> None:
    sampler = Sampler()
    sampler.start()
    # One reading exists immediately; the second arrives on the next tick, so
    # the first ~25 seconds of rows honestly say "no CPU sample yet".
    server = ThreadingHTTPServer((host, port), handler_for(sampler))
    echo(f"local:   http://127.0.0.1:{port}/")
    if exposed:
        echo(f"phone:   {expose(port)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        server.server_close()


# Installable as a phone app (Chrome also needs HTTPS: tailscale serve --https=18787).
MANIFEST = json.dumps({
    "name": "Claude sessions", "short_name": "Sessions", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#0e1116", "theme_color": "#0e1116",
    "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
})
ICON = Path(__file__).with_name("icon-512.png")

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Claude sessions</title>
<link rel="icon" href="data:,">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-512.png">
<meta name="theme-color" content="#0e1116">
<style>
  :root {
    --bg: #0e1116; --card: #171b22; --line: #262c36; --ink: #e6edf3;
    --dim: #8b949e; --red: #f85149; --amber: #d29922; --green: #3fb950;
    --blue: #58a6ff; --grey: #6e7681;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
  header { padding: 18px 16px 12px; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--bg); z-index: 2; }
  h1 { margin: 0 0 10px; font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .totals { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--dim); }
  .totals b { color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 600; }
  main { padding: 12px 12px 40px; display: flex; flex-direction: column; gap: 10px; }
  .row { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--grey);
    border-radius: 10px; padding: 12px 13px; }
  .row.running { border-left-color: var(--amber); }
  .row.idle, .row.finished { border-left-color: var(--green); }
  .row.waiting { border-left-color: var(--blue); }
  .row.unknown { border-left-color: var(--grey); }
  .row.protected { border-left-color: var(--red); background: #1d1416; border-color: #40232a; }
  .top { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .chip { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    padding: 3px 7px; border-radius: 5px; background: #21262d; color: var(--dim); white-space: nowrap; }
  .chip.running { background: #3b2c0c; color: #e3b341; }
  .chip.idle, .chip.finished { background: #12331d; color: var(--green); }
  .chip.waiting { background: #102b4d; color: var(--blue); }
  .chip.protected { background: #3d1519; color: #ff7b72; }
  .mem { margin-left: auto; font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
  .title { margin: 7px 0 3px; font-size: 14px; overflow: hidden; text-overflow: ellipsis;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .meta { font-size: 12px; color: var(--dim); word-break: break-word; }
  .why { font-size: 12px; color: var(--dim); margin-top: 6px; font-style: italic; }
  .actions { margin-top: 10px; display: flex; gap: 8px; align-items: center; }
  button { font: inherit; font-size: 13px; font-weight: 600; border-radius: 8px; padding: 8px 14px;
    border: 1px solid var(--line); background: #21262d; color: var(--ink); cursor: pointer; }
  button.go { background: #12331d; border-color: #1f6f3a; color: #6ee787; }
  button.danger { background: #3d1519; border-color: #6e2b31; color: #ff9d95; }
  button:disabled { opacity: .45; }
  .noact { font-size: 12px; color: var(--dim); margin-top: 9px; }
  .bulk { padding: 0 12px; }
  .bulk button { width: 100%; padding: 12px; }
  .flash { margin: 0 12px 6px; padding: 9px 12px; border-radius: 8px; font-size: 13px;
    background: #12331d; color: #6ee787; }
  .flash.bad { background: #3d1519; color: #ff9d95; }
  footer { padding: 6px 16px 28px; font-size: 12px; color: var(--dim); }
  @media (min-width: 720px) { main { max-width: 860px; margin: 0 auto; } .bulk { max-width: 860px; margin: 0 auto; } }
</style>
</head>
<body>
<header>
  <h1>Claude sessions on this box</h1>
  <div class="totals" id="totals"></div>
</header>
<div id="flash"></div>
<div class="bulk" id="bulk"></div>
<main id="rows"></main>
<footer id="foot"></footer>
<script>
const $ = (id) => document.getElementById(id);
let armed = null;          // pid awaiting a confirm tap
let busy = false;          // a reap is in flight; do not repaint under it
let bulkArmed = false;

function ago(s) {
  if (s == null) return "?";
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 172800) return (s / 3600).toFixed(1) + "h";
  return Math.round(s / 86400) + "d";
}
function mb(v) { return v >= 1024 ? (v / 1024).toFixed(1) + " GB" : v + " MB"; }

function flash(text, bad) {
  $("flash").innerHTML = text ? `<div class="flash ${bad ? "bad" : ""}">${text}</div>` : "";
  if (text) setTimeout(() => { $("flash").innerHTML = ""; }, 6000);
}

async function reap(pid, fingerprint) {
  busy = true;
  try {
    const res = await fetch("/api/reap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pid, fingerprint }),
    });
    const data = await res.json();
    flash(data.ok ? data.message : data.error, !data.ok);
    return data.ok;
  } catch (e) {
    flash(String(e), true);
    return false;
  } finally {
    busy = false;
    armed = null;
  }
}

function render(data) {
  const t = data.totals;
  $("totals").innerHTML = [
    `claude holding <b>${mb(t.claudeMb)}</b>`,
    `free <b>${mb(t.memAvailableMb)}</b> of ${mb(t.memTotalMb)}`,
    `reapable <b>${mb(t.reapableMb)}</b>`,
  ].map((s) => `<span>${s}</span>`).join("");

  const reapables = data.sessions.filter((s) => s.reapable);
  $("bulk").innerHTML = reapables.length
    ? `<button class="${bulkArmed ? "danger" : "go"}" id="bulkbtn">${
        bulkArmed
          ? `Tap again to close ${reapables.length} session${reapables.length > 1 ? "s" : ""}`
          : `Reap ${reapables.length} idle · ${mb(t.reapableMb)}`
      }</button>`
    : "";
  const bb = $("bulkbtn");
  if (bb) bb.onclick = async () => {
    if (!bulkArmed) { bulkArmed = true; render(data); setTimeout(() => { bulkArmed = false; }, 5000); return; }
    bulkArmed = false;
    for (const s of reapables) await reap(s.pid, s.fingerprint);
    poll();
  };

  $("rows").innerHTML = data.sessions.map((s) => {
    const cls = s.protected ? "protected" : s.state;
    const chip = s.protected ? "protected" : s.state;
    const chipText = s.protected ? "never reap" : s.state;
    const bits = [];
    if (s.jobId) bits.push(`job ${s.jobId}${s.jobStatus ? " · " + s.jobStatus : ""}`);
    bits.push(s.where);
    if (s.tmux) bits.push(s.tmux);
    bits.push("pid " + s.pid);
    bits.push("up " + ago(s.ageSeconds));
    if (s.childCount) bits.push(s.childCount + " child procs");
    let action;
    if (s.protected) {
      action = `<div class="noact">Protected — ${
        s.kind === "concierge"
          ? "this is what answers your messages"
          : "this is what puts the machine in the Claude app"
      }. Never closed from here.</div>`;
    } else if (s.reapable) {
      action = `<div class="actions"><button class="${armed === s.pid ? "danger" : "go"}" data-pid="${s.pid}" data-fp="${s.fingerprint}">${
        armed === s.pid ? "Tap again to close" : "Reap · " + mb(s.rssTreeMb)
      }</button></div>`;
    } else {
      action = `<div class="noact">No button — ${s.state === "unknown" ? "not measured yet" : "still going"}.</div>`;
    }
    return `<div class="row ${cls}">
      <div class="top"><span class="chip ${chip}">${chipText}</span>
        <span class="mem">${mb(s.rssTreeMb)}</span></div>
      <div class="title">${escapeHtml(s.title || "(untitled)")}</div>
      <div class="meta">${bits.map(escapeHtml).join(" · ")}</div>
      <div class="why">${escapeHtml(s.why)}</div>
      ${action}
    </div>`;
  }).join("");

  for (const btn of $("rows").querySelectorAll("button[data-pid]")) {
    btn.onclick = async () => {
      const pid = Number(btn.dataset.pid);
      if (armed !== pid) { armed = pid; render(data); setTimeout(() => { if (armed === pid) { armed = null; } }, 5000); return; }
      btn.disabled = true;
      await reap(pid, Number(btn.dataset.fp));
      poll();
    };
  }

  $("foot").textContent =
    `${data.helpers.count} short-lived helper process${data.helpers.count === 1 ? "" : "es"} holding ${mb(data.helpers.rssMb)}` +
    ` · sizes are RSS of the session and its MCP servers, which overlap, so expect to get back roughly half` +
    ` · reap grace ${data.graceMinutes} min · refreshed ${new Date().toLocaleTimeString()}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function poll() {
  if (busy) return;
  try {
    const res = await fetch("/api/sessions");
    render(await res.json());
  } catch (e) {
    flash("lost the server — " + e, true);
  }
}
poll();
setInterval(poll, 5000);
</script>
</body>
</html>
"""
