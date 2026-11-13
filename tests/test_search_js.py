"""The tasks page searches exactly like Tickfind: search.js against tickfind/tests/search-cases.json,
the checks every search implementation runs."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(os.environ.get("TICKFIND_HOME", ROOT.parent / "tickfind")) / "tests/search-cases.json"
HARNESS = r"""
const {run} = require(process.argv[1]), cases = require(process.argv[2]).cases, today = "2026-10-16";
const now = Date.parse("2026-10-16T09:00:00Z"), day = 864e5, failed = [];
const plus = n => new Date(Date.parse(today + "T00:00:00Z") + n * day).toISOString().slice(0, 10);
for (const c of cases) {
  const tasks = c.tasks.map(j => ({title: j.title, content: j.content || "", items: j.items || [], tags: j.tags || [],
    list: j.list || "Inbox", kind: j.kind || "TEXT", priority: j.priority || 0, day: j.dueDays === undefined ? null : plus(j.dueDays),
    modified: now - (j.ageDays || 0) * day, created: now - (j.ageDays || 0) * day}));
  for (const k of c.checks) {
    const got = run(tasks, k.q, {sort: k.sort || "priority", today, filter: k.filter || ""}).map(t => t.title);
    if (JSON.stringify(got) !== JSON.stringify(k.want)) failed.push(`${c.name}: "${k.q}" gave ${JSON.stringify(got)}`);
  }
}
console.log(JSON.stringify(failed));
"""


@pytest.mark.skipif(not CASES.exists() or not shutil.which("node"), reason="needs node and the tickfind repo beside this one")
def test_search_js_passes_every_shared_search_case():
    out = subprocess.run(["node", "-e", HARNESS, str(ROOT / "concierge/search.js"), str(CASES)],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == []
