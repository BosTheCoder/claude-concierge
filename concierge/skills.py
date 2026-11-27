"""The skills a job can run, for the app's "/" autocomplete, and the hint that makes a job run one.

A job gets his message as `[thread N] Kelvin says: /x ...`, not at the start of its input box, so Claude
Code never treats it as a slash command. Instead the job is told to run the skill with the Skill tool.
Skills marked `disable-model-invocation` can't be run that way, so they are left out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CLAUDE = Path.home() / ".claude"
SLASH = re.compile(r"^/([\w:.-]+)(?:\s|$)")


def _front(path: Path) -> dict:
    """The SKILL.md / command frontmatter, flat: `key: value`, with folded (>, |) values joined."""
    lines = path.read_text(errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if key and line[:1] in (" ", "\t"):
            out[key] = (out[key] + " " + line.strip()).strip()
        elif m := re.match(r"^([\w-]+):\s*(.*)$", line):
            key, val = m.group(1), m.group(2).strip()
            out[key] = "" if val in (">", "|", ">-", "|-") else val.strip("\"'")
    return out


def _entry(path: Path, name: str) -> dict | None:
    f = _front(path)
    if f.get("disable-model-invocation") == "true" or f.get("user-invocable") == "false":
        return None
    desc = f.get("description", "")
    return {"name": name, "description": desc[:160] + ("…" if len(desc) > 160 else "")}


def _live_root(home: Path, key: str) -> Path | None:
    """A plugin from a directory marketplace (his ai-toolkit) runs from that folder, not the stale cache copy."""
    plugin, market = key.split("@")
    try:
        src = json.loads((home / "plugins" / "known_marketplaces.json").read_text())[market]["source"]
        if src.get("source") != "directory":
            return None
        listing = json.loads((Path(src["path"]) / ".claude-plugin" / "marketplace.json").read_text())
        rel = next(p["source"] for p in listing["plugins"] if p["name"] == plugin)
        return (Path(src["path"]) / rel).resolve() if isinstance(rel, str) else None
    except (OSError, ValueError, KeyError, StopIteration):
        return None


def available(home: Path = CLAUDE) -> list[dict]:
    """Every enabled plugin's skills and commands (as `plugin:name`), plus ~/.claude/skills."""
    found: dict[str, dict] = {}
    try:
        enabled = json.loads((home / "settings.json").read_text()).get("enabledPlugins", {})
        installed = json.loads((home / "plugins" / "installed_plugins.json").read_text()).get("plugins", {})
    except (OSError, ValueError):
        enabled, installed = {}, {}
    for key, installs in installed.items():
        if not enabled.get(key) or not installs:
            continue
        plugin, root = key.split("@")[0], _live_root(home, key) or Path(installs[-1]["installPath"])
        for p in sorted(root.glob("skills/*/SKILL.md")) + sorted(root.glob("commands/*.md")):
            name = f"{plugin}:{p.parent.name if p.name == 'SKILL.md' else p.stem}"
            if e := _entry(p, name):
                found.setdefault(name, e)
    for p in sorted((home / "skills").glob("**/SKILL.md")):
        if e := _entry(p, p.parent.name):
            found.setdefault(p.parent.name, e)
    return sorted(found.values(), key=lambda e: e["name"])


def resolve(word: str, known: list[dict]) -> str | None:
    """`research` or `ai-toolkit:research` → the full name, if exactly one skill matches."""
    names = [e["name"] for e in known]
    if word in names:
        return word
    hits = [n for n in names if n.split(":")[-1] == word]
    return hits[0] if len(hits) == 1 else None


def hint(text: str, known: list[dict] | None = None) -> str:
    """For a message that starts with /skill: the line telling the job to run it. Else ''."""
    m = SLASH.match(text.strip())
    name = m and resolve(m.group(1), available() if known is None else known)
    if not name:
        return ""
    return (f"\n(He started it with /{m.group(1)}: run the `{name}` skill with the Skill tool, "
            f"with the rest of his message as its args.)")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        root = home / "cache" / "kit"
        (root / "skills" / "research").mkdir(parents=True)
        (root / "skills" / "research" / "SKILL.md").write_text("---\nname: research\ndescription: >\n  Find\n  things\n---\n")
        (root / "skills" / "ship").mkdir()
        (root / "skills" / "ship" / "SKILL.md").write_text("---\nname: ship\ndisable-model-invocation: true\n---\n")
        (home / "plugins").mkdir()
        (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"kit@m": True}}))
        (home / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"kit@m": [{"installPath": str(root)}]}}))
        got = available(home)
        assert got == [{"name": "kit:research", "description": "Find things"}], got
        assert "`kit:research`" in hint("/research best pizza", got)
        assert hint("/ship it", got) == "" and hint("no slash", got) == "" and hint("/nope", got) == ""
    print("ok")
