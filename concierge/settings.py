"""Everything that differs between one installation and the next.

The rest of this package is machine-agnostic; the parts that are not — which
repos a job may run in, where their GitHub mirrors are, what the fast lanes
run, what the heartbeat watches — live in `concierge.toml` at the repo root.
That file is gitignored. `concierge.example.toml` is the committed template.

Loading is deliberately forgiving about a *missing* file and loud about a
*broken* one. A fresh clone with no `concierge.toml` falls back to the example
so the CLI still starts, the tests still run, and `--help` still works; the
first thing that actually needs a real repo path fails with a message that
names the file to write. A malformed TOML, on the other hand, is a mistake
someone made two minutes ago and wants to hear about immediately.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "concierge.toml"
EXAMPLE_FILE = REPO_ROOT / "concierge.example.toml"

# Point at a different config file. The test suite uses this to run against a
# fixture instead of whatever the machine happens to be configured for.
CONFIG_ENV_VAR = "CONCIERGE_CONFIG"


class ConfigError(RuntimeError):
    """The config file is missing something the caller genuinely needs."""


@dataclass(frozen=True)
class Repo:
    """One repo a job is allowed to run in.

    `topics` is prose, not a matcher — it is pasted into the concierge's system
    prompt so the model can route by meaning. Routing is a judgement call, and
    a keyword list would only be a worse version of the same guess.
    """

    name: str
    path: Path
    github: str | None = None
    topics: str = ""
    default: bool = False


@dataclass(frozen=True)
class LaneSpec:
    name: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class WatchSpec:
    name: str
    url: str
    kind: str
    max_age_hours: float
    expected: str


@dataclass(frozen=True)
class RemoteControl:
    session: str = "rc"
    window: str = "0"
    name: str = ""
    spawn_mode: str = "same-dir"


@dataclass(frozen=True)
class Settings:
    repos: tuple[Repo, ...] = ()
    lanes: tuple[LaneSpec, ...] = ()
    watches: tuple[WatchSpec, ...] = ()
    tmux_session: str = "concierge"
    remote_control: RemoteControl = field(default_factory=RemoteControl)
    source: Path | None = None
    is_example: bool = True

    @property
    def default_repo(self) -> Repo:
        """Where the concierge itself runs, and where a job goes when the topic
        doesn't clearly belong anywhere else."""
        for repo in self.repos:
            if repo.default:
                return repo
        if self.repos:
            return self.repos[0]
        raise ConfigError(
            f"no repos configured — copy {EXAMPLE_FILE.name} to "
            f"{CONFIG_FILE.name} and set at least one [[repos]] entry"
        )

    def repo_paths(self) -> set[Path]:
        """The permitted set, resolved. A cwd outside it is refused at spawn."""
        return {repo.path.resolve() for repo in self.repos}


def _expand(raw: str) -> Path:
    return Path(raw).expanduser()


def _parse_repo(row: dict, index: int) -> Repo:
    try:
        name, path = row["name"], row["path"]
    except KeyError as exc:
        raise ConfigError(f"[[repos]] #{index + 1} has no {exc.args[0]}") from exc
    return Repo(
        name=name,
        path=_expand(path),
        github=(row.get("github") or "").rstrip("/") or None,
        topics=row.get("topics", ""),
        default=bool(row.get("default", False)),
    )


def _parse_lane(row: dict, index: int) -> LaneSpec:
    try:
        name, command = row["name"], row["command"]
    except KeyError as exc:
        raise ConfigError(f"[[lanes]] #{index + 1} has no {exc.args[0]}") from exc
    if not command:
        raise ConfigError(f"[[lanes]] {name} has an empty command")
    # The first element is a path we test for existence, so it gets expanded;
    # the rest are the lane's own arguments and are passed through untouched.
    head, *rest = command
    return LaneSpec(name=name, command=(str(_expand(head)), *rest))


def _parse_watch(row: dict, index: int) -> WatchSpec:
    missing = [k for k in ("name", "url", "kind") if k not in row]
    if missing:
        raise ConfigError(f"[[watches]] #{index + 1} has no {', '.join(missing)}")
    return WatchSpec(
        name=row["name"],
        url=row["url"],
        kind=row["kind"],
        max_age_hours=float(row.get("max_age_hours", 30)),
        expected=row.get("expected", "on its usual schedule"),
    )


def _source_file(path: Path | None) -> tuple[Path | None, bool]:
    if path is not None:
        return path, False
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser(), False
    if CONFIG_FILE.exists():
        return CONFIG_FILE, False
    if EXAMPLE_FILE.exists():
        return EXAMPLE_FILE, True
    return None, True


def load(path: Path | None = None) -> Settings:
    """Read the config. Never raises on a missing file; always on a broken one."""
    source, is_example = _source_file(path)
    if source is None:
        return Settings()

    try:
        raw = tomllib.loads(source.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from exc

    rc = raw.get("remote_control") or {}
    return Settings(
        repos=tuple(
            _parse_repo(row, i) for i, row in enumerate(raw.get("repos") or [])
        ),
        lanes=tuple(
            _parse_lane(row, i) for i, row in enumerate(raw.get("lanes") or [])
        ),
        watches=tuple(
            _parse_watch(row, i) for i, row in enumerate(raw.get("watches") or [])
        ),
        tmux_session=(raw.get("tmux") or {}).get("session", "concierge"),
        remote_control=RemoteControl(
            session=rc.get("session", "rc"),
            window=str(rc.get("window", "0")),
            # The CLI would derive the same thing from the hostname; naming it
            # here means the tests can assert on it.
            name=rc.get("name") or socket.gethostname(),
            spawn_mode=rc.get("spawn_mode", "same-dir"),
        ),
        source=source,
        is_example=is_example,
    )


@lru_cache(maxsize=1)
def current() -> Settings:
    """The process-wide settings. Cached — the CLI is short-lived and the
    tests that care pass their own file to `load` instead."""
    return load()


# --- prompt rendering -------------------------------------------------------
#
# The system prompts are committed, so they cannot name anyone's repos. They
# carry placeholders instead, and the installation's own values are pasted in
# at the moment a session is started.

PROMPTS_DIR = REPO_ROOT / "prompts"
RENDERED_DIR = REPO_ROOT / "state"


def routing_block(settings: Settings) -> str:
    """The 'pick the repo' bullets of the concierge prompt."""
    if not settings.repos:
        return (
            "   No repos are configured. Say so instead of spawning, and point\n"
            "   at concierge.toml."
        )
    default = settings.default_repo
    lines = [
        f"   - `{repo.path}`" + (f" — {repo.topics}" if repo.topics else "")
        for repo in settings.repos
    ]
    lines.append("")
    lines.append(
        f"   If you are unsure, use `{default.path}`; a repo's own CLAUDE.md "
        f"will redirect you if it turns out to be the wrong one."
    )
    return "\n".join(lines)


def render_prompt(name: str, settings: Settings | None = None) -> Path:
    """Substitute this installation's values into a prompt and write it out.

    Returns the path to pass to `--append-system-prompt-file`. The rendered
    copy lives in `state/` beside the registry, which is already gitignored.
    """
    settings = settings or current()
    template = (PROMPTS_DIR / f"{name}.md").read_text()
    body = template.replace("{{REPO_ROUTING}}", routing_block(settings)).replace(
        "{{CONCIERGE_BIN}}", str(REPO_ROOT / "bin" / "concierge")
    )
    RENDERED_DIR.mkdir(parents=True, exist_ok=True)
    out = RENDERED_DIR / f"{name}.rendered.md"
    out.write_text(body)
    return out
