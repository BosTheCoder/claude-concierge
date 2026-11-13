// Tickfind's search, for the tasks page: a port of tickfind/windows/Search.cs (Run and what it needs).
// Every implementation passes tickfind/tests/search-cases.json (tests/test_search_js.py runs this one),
// so change search by adding a check there first.
//
// run(tasks, text, {sort, today, filter}) → tasks in order. Each task: {title, content, items[], tags[], list,
// kind, priority, day ("YYYY-MM-DD" or null, its due day), modified, created (ms)}. today: "YYYY-MM-DD".
(function (exports) {
  const fold = s => String(s || "").toLowerCase().normalize("NFD").replace(/\p{Mn}/gu, "");
  const PRI = {high: 5, h: 5, 5: 5, medium: 3, med: 3, m: 3, 3: 3, low: 1, l: 1, 1: 1, none: 0, n: 0, 0: 0};
  const SORTS = ["priority", "best", "due", "modified", "created"];
  const SEP = /[ \n\t.,;:!?()[\]{}<>"'/\\|\-_*#@&+=~`]+/;

  function tokens(text) {  // splits on spaces, keeping "quoted phrases" together
    const out = []; let b = "", quoted = false;
    for (const c of String(text || "")) {
      if (c === '"') { quoted = !quoted; continue; }
      if (/\s/.test(c) && !quoted) { if (b) out.push(b); b = ""; } else b += c;
    }
    if (b) out.push(b);
    return out;
  }
  const query = () => ({terms: [], not: [], tags: [], lists: [], notLists: [], pri: new Set(), due: new Set(), kind: null, sort: null});
  function add(q, raw) {
    const w = fold(raw);
    if (w.length > 1 && w[0] === "#") q.tags.push(w.slice(1));
    else if (w.length > 1 && w[0] === "~") q.lists.push(w.slice(1));
    else if (w.startsWith("list:") && w.length > 5) q.lists.push(w.slice(5));
    else if (w.length > 2 && w.startsWith("-~")) q.notLists.push(w.slice(2));
    else if (w.length > 1 && w[0] === "!" && w.slice(1) in PRI) q.pri.add(PRI[w.slice(1)]);
    else if (w.startsWith("is:") && w.length > 3) q.kind = w.slice(3);
    else if (w.startsWith("due:") && w.length > 4) q.due.add(w.slice(4));
    else if (w === "has:due") q.due.add("any");
    else if (w.startsWith("sort:") && (SORTS.includes(w.slice(5)) || w.slice(5) === "relevance")) q.sort = w.slice(5) === "relevance" ? "best" : w.slice(5);
    else if (w.length > 1 && w[0] === "-") q.not.push(w.slice(1));
    else if (w) q.terms.push(w);
  }
  function alternatives(text, filter) {  // one query per side of each "|", the filter on every side
    const sides = [[]];
    for (const raw of tokens(text)) raw === "|" ? sides.push([]) : sides[sides.length - 1].push(raw);
    const kept = sides.filter(s => s.length);
    return (kept.length ? kept : [[]]).map(side => { const q = query(); for (const raw of [...tokens(filter), ...side]) add(q, raw); return q; });
  }

  const norm = t => t._n || (t._n = {title: fold(t.title), body: fold((t.content || "") + "\n" + (t.items || []).join("\n")),
                                     list: fold(t.list), tags: (t.tags || []).map(fold)});
  const plusDays = (day, n) => { const d = new Date(day + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };

  function passes(t, q, today) {
    const n = norm(t);
    if (q.tags.some(g => !n.tags.some(x => x.startsWith(g)))) return false;
    if (q.lists.some(l => !n.list.includes(l)) || q.notLists.some(l => n.list.includes(l))) return false;
    if (q.pri.size && !q.pri.has(t.priority || 0)) return false;
    if (q.not.some(w => n.title.includes(w) || n.body.includes(w) || n.tags.some(x => x.includes(w)))) return false;
    if (q.kind !== null && !fold(t.kind || "TEXT").startsWith(q.kind === "task" ? "text" : q.kind)) return false;
    if (q.due.size) {
      const d = t.day || null;
      const ok = k => k === "any" ? d !== null : k === "none" ? d === null : d === null ? false
        : k === "today" ? d === today : k === "tomorrow" ? d === plusDays(today, 1) : k === "overdue" ? d < today
        : k === "week" ? d >= today && d < plusDays(today, 7) : true;
      if (![...q.due].some(ok)) return false;
    }
    return true;
  }

  const wordStart = (s, i) => i === 0 || !/[\p{L}\p{N}]/u.test(s[i - 1]);
  function exact(n, term) {
    let s = 0, i = n.title.indexOf(term);
    if (i >= 0) s += 10 + (wordStart(n.title, i) ? 5 : 0) + (i === 0 ? 3 : 0);
    if (n.tags.some(x => x.startsWith(term))) s += 6; else if (n.tags.some(x => x.includes(term))) s += 4;
    if (n.list.includes(term)) s += 3;
    i = n.body.indexOf(term);
    if (i >= 0) s += 2 + (wordStart(n.body, i) ? 1 : 0);
    return s;
  }
  function lev1(a, b) {  // edit distance ≤ 1, a swap of neighbours counting as one
    if (Math.abs(a.length - b.length) > 1) return false;
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    if (i === a.length && i === b.length) return true;
    if (a.length === b.length)
      return a.slice(i + 1) === b.slice(i + 1) || (i + 1 < a.length && a[i] === b[i + 1] && a[i + 1] === b[i] && a.slice(i + 2) === b.slice(i + 2));
    return a.length > b.length ? a.slice(i + 1) === b.slice(i) : a.slice(i) === b.slice(i + 1);
  }
  const close = (term, word) => lev1(term, word) || (word.length > term.length && lev1(term, word.slice(0, term.length)));
  const words = s => s.split(SEP).filter(Boolean);
  function fuzzy(n, term) {  // one typo, in words of four letters or more
    if (term.length < 4) return exact(n, term);
    if (words(n.title).some(w => close(term, w))) return 4;
    if (n.tags.some(x => close(term, x)) || words(n.body).some(w => close(term, w))) return 1;
    return 0;
  }
  function score(t, q, isFuzzy) {
    const n = norm(t); let total = 0;
    for (const term of q.terms) { const s = isFuzzy ? fuzzy(n, term) : exact(n, term); if (!s) return null; total += s; }
    if (q.terms.length > 1 && !isFuzzy && n.title.includes(q.terms.join(" "))) total += 8;
    return total;
  }
  function match(tasks, alts, today, isFuzzy) {
    const hits = [];
    for (const t of tasks) {
      let best = null;
      for (const q of alts) if (passes(t, q, today)) { const s = score(t, q, isFuzzy); if (s !== null && (best === null || s > best)) best = s; }
      if (best !== null) hits.push({t, s: best});
    }
    return hits;
  }
  const by = (...keys) => (a, b) => { for (const k of keys) { const d = k(a, b); if (d) return d; } return 0; };
  const desc = f => (a, b) => (f(b) > f(a)) - (f(b) < f(a));
  const asc = f => (a, b) => (f(a) > f(b)) - (f(a) < f(b));
  const ORDER = {
    best: by(desc(h => h.s), desc(h => h.t.priority || 0), desc(h => h.t.modified || 0)),
    due: by(asc(h => h.t.day === null || h.t.day === undefined), asc(h => h.t.day || ""), desc(h => h.t.priority || 0), desc(h => h.s)),
    modified: desc(h => h.t.modified || 0),
    created: desc(h => h.t.created || 0),
    priority: by(desc(h => h.t.priority || 0), desc(h => h.s), desc(h => h.t.modified || 0)),
  };

  exports.run = function (tasks, text, {sort = "priority", today, filter = ""} = {}) {
    const alts = alternatives(text, filter);
    let hits = match(tasks, alts, today, false);
    if (!hits.length && alts.some(q => q.terms.length)) hits = match(tasks, alts, today, true);
    const chosen = alts.map(q => q.sort).find(Boolean) || sort;
    return hits.sort(ORDER[chosen]).map(h => h.t);  // Array.sort is stable, like LINQ's OrderBy
  };
  exports.fold = fold;
})(typeof module === "object" ? module.exports : (window.TickSearch = {}));
