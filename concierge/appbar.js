// The bottom bar of his one phone app (Concierge): every productivity page on the tailnet's main
// address is a pane of it. A page joins with one line: <script src="/claude/appbar.js" defer></script>
// That also points the page at the app's manifest, so installing from any pane installs the one app.
// Fixed-bottom things on a page sit above the bar with bottom: calc(var(--appbar, 0px) + ...).
(() => {
  // Ctrl/Cmd+Enter in a text box presses the main button after it (Send, Start, Save), on every pane.
  addEventListener("keydown", e => {
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || e.isComposing || !e.target.matches?.("textarea, input")) return;
    if (e.target.form) { e.preventDefault(); e.target.form.requestSubmit(); return; }
    for (let el = e.target.parentElement; el; el = el.parentElement) {
      const b = [...el.querySelectorAll("button.btn:not(.ghost)")]
        .find(b => !b.disabled && e.target.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      if (b) { e.preventDefault(); b.click(); return; }
    }
  });
  if (location.port || document.getElementById("appbar")) return;  // :18787 etc. are other addresses
  // Every pane, by id. Which sit in the bar, in what order, is his: Concierge → Settings → Bottom bar
  // saves {order, main} to the concierge (kv "appbar"); this page keeps a copy so the bar draws at once.
  const PANES = [
    {id: "concierge", href: "/claude/app", icon: "✦", name: "Concierge"},
    {id: "tasks", href: "/claude/tasks", icon: "☑", name: "Tasks"},
    {id: "inbox", href: "/inbox/", icon: "✉", name: "Inbox"},
    // Plannotator Inbox, framed so it stays in the app (it lives on :45581, owner-only).
    {id: "review", href: "/claude/review", icon: "✎", name: "Review"},
    {id: "brief", href: "/brief/", icon: "☀", name: "Brief"},
    {id: "money", href: "/money/", icon: "£", name: "Money"},
    // The vault browser's viewer, proxied by the concierge so it opens inside the app.
    // Its controls (restart, tabs) are on the Apps page and the icon's long-press menu.
    {id: "browser", href: "/claude/vault/", icon: "◳", name: "Browser"},
    {id: "apps", href: "/", icon: "⌂", name: "Apps"},  // the tailnet home page: every app he has
  ];
  const DEFAULT = {order: PANES.map(p => p.id), main: 4};
  const KEY = "appbar-layout", API = "/claude/api/settings/appbar";
  // Saved order first, then any pane added since (it lands at the end, under More).
  const arrange = cfg => {
    const byId = Object.fromEntries(PANES.map(p => [p.id, p]));
    const order = [...new Set([...(cfg.order || []).filter(id => byId[id]), ...PANES.map(p => p.id)])];
    return {panes: order.map(id => byId[id]), main: Math.min(Math.max(+cfg.main || DEFAULT.main, 1), 6)};
  };
  const cached = () => { try { return JSON.parse(localStorage.getItem(KEY)) || DEFAULT; } catch { return DEFAULT; } };
  const remember = cfg => { try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch {} };
  // For the settings page: the catalogue, the layout in force, and saving a new one.
  window.appbar = {
    panes: PANES, layout: () => arrange(cached()),
    async save(cfg) {
      const r = await fetch(API, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      remember(cfg); render(arrange(cfg));
    },
  };
  // On every screen: it's how he moves between panes. Only a touch keyboard pushes it aside.
  const touch = matchMedia("(pointer: coarse)").matches;
  const trim = s => s.replace(/\/$/, ""), path = trim(location.pathname);
  const here = PANES.find(({href: p}) => p === "/" ? path === "" : path === trim(p) || path.startsWith(trim(p) + "/"));
  const css = document.createElement("style");
  css.textContent = `
    #appbar { position:fixed; left:0; right:0; bottom:0; z-index:2147483000; display:flex; justify-content:space-around;
      padding:4px 4px calc(env(safe-area-inset-bottom) + 4px); background:#14161a; border-top:1px solid #2c3038;
      font:11px/1.2 system-ui, -apple-system, sans-serif }
    #appbar a { flex:1; min-width:0; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; display:flex; flex-direction:column; align-items:center; gap:2px; min-height:48px; justify-content:center;
      color:#9aa0a8; text-decoration:none; border-radius:12px }
    #appbar a b { font-size:20px; font-weight:400; line-height:1 }
    #appbar a[aria-current=page] { color:#8ab4f8; font-weight:600 }
    #appbar a:focus-visible { outline:2px solid #8ab4f8 }
    html.appbar-light #appbar { background:#f6f5f2; border-top-color:#e6e3dd }
    html.appbar-light #appbar a { color:#6b6f76 } html.appbar-light #appbar a[aria-current=page] { color:#1a73e8 }
    #appbar { transition:transform .2s ease }
    html.appbar-off #appbar { display:none }
    html.appbar-tucked #appbar { transform:translateY(110%) }
    @media (min-width:700px) { #appbar { left:50%; right:auto; width:min(640px, 100vw); transform:translateX(-50%);
      border:1px solid #2c3038; border-bottom:0; border-radius:16px 16px 0 0 }
      html.appbar-tucked #appbar { transform:translate(-50%, 110%) } }
    body { padding-bottom:var(--appbar-room, 0px) !important }
    #appbar button { all:unset; flex:1; min-width:0; display:flex; flex-direction:column; align-items:center; gap:2px; min-height:48px; justify-content:center;
      color:#9aa0a8; border-radius:12px; cursor:pointer; font:inherit }
    #appbar button b { font-size:20px; font-weight:400; line-height:1 }
    #appbar button[aria-current=page], #appbar button[aria-expanded=true] { color:#8ab4f8; font-weight:600 }
    html.appbar-light #appbar button { color:#6b6f76 } html.appbar-light #appbar button[aria-current=page],
    html.appbar-light #appbar button[aria-expanded=true] { color:#1a73e8 }
    #appbar-more { position:fixed; left:8px; right:8px; bottom:calc(var(--appbar, 60px) + 8px); z-index:2147483001;
      display:grid; grid-template-columns:repeat(4, 1fr); gap:4px; padding:10px 6px; border-radius:18px;
      background:#1d2026; border:1px solid #2c3038; box-shadow:0 8px 30px rgba(0,0,0,.45);
      font:12px/1.2 system-ui, -apple-system, sans-serif }
    #appbar-more[hidden] { display:none }
    #appbar-more a { display:flex; flex-direction:column; align-items:center; gap:6px; padding:12px 2px; min-height:48px;
      color:#c8ccd2; text-decoration:none; border-radius:12px }
    #appbar-more a b { font-size:24px; font-weight:400; line-height:1 }
    #appbar-more a[aria-current=page] { color:#8ab4f8; font-weight:600; background:#262a31 }
    html.appbar-light #appbar-more { background:#fff; border-color:#e6e3dd; box-shadow:0 8px 30px rgba(0,0,0,.15) }
    html.appbar-light #appbar-more a { color:#3c4043 } html.appbar-light #appbar-more a[aria-current=page] { color:#1a73e8; background:#eef3fd }
    #appbar-scrim { position:fixed; inset:0; z-index:2147483000; background:rgba(0,0,0,.25) } #appbar-scrim[hidden] { display:none }`;
  const nav = document.createElement("nav");
  nav.id = "appbar";
  nav.setAttribute("aria-label", "Apps");
  const link_ = p => `<a href="${p.href}"${p === here ? ' aria-current="page"' : ""}><b aria-hidden="true">${p.icon}</b>${p.name}</a>`;
  // More: a sheet above the bar with the other panes, closed by a tap outside, Escape, or More again.
  const sheet = document.createElement("div"), scrim = document.createElement("div");
  sheet.id = "appbar-more"; sheet.hidden = true;
  scrim.id = "appbar-scrim"; scrim.hidden = true;
  let more = null;
  const openMore = on => {
    if (!more) return;
    sheet.hidden = scrim.hidden = !on; more.setAttribute("aria-expanded", String(on));
    if (on) sheet.querySelector("a")?.focus();
  };
  const render = ({panes, main}) => {
    const all = panes.length <= main + 1;  // one spare pane goes in the bar rather than behind More
    const bar = all ? panes : panes.slice(0, main), rest = all ? [] : panes.slice(main);
    const inRest = rest.includes(here);
    nav.innerHTML = bar.map(link_).join("") + (rest.length ?
      `<button type="button" aria-haspopup="true" aria-expanded="false" aria-controls="appbar-more"${inRest ? ' aria-current="page"' : ""}>` +
      `<b aria-hidden="true">${inRest ? here.icon : "⋯"}</b>${inRest ? here.name : "More"}</button>` : "");
    sheet.innerHTML = rest.map(link_).join("");
    sheet.hidden = scrim.hidden = true;
    more = nav.querySelector("button");
    more?.addEventListener("click", () => openMore(sheet.hidden));
  };
  render(arrange(cached()));
  // The concierge's copy wins: one layout on every device.
  fetch(API).then(r => r.ok ? r.json() : null).then(cfg => {
    if (!cfg || !cfg.order) return;
    if (JSON.stringify(cfg) !== JSON.stringify(cached())) { remember(cfg); render(arrange(cfg)); }
  }).catch(() => {});
  scrim.addEventListener("click", () => openMore(false));
  addEventListener("keydown", e => { if (e.key === "Escape" && !sheet.hidden) { openMore(false); more?.focus(); } });
  addEventListener("pageshow", () => openMore(false));  // coming Back to a page leaves no sheet open
  const root = document.documentElement;
  // While he types the keyboard is up: the bar steps aside so it doesn't sit between the text and the keys.
  const typing = () => { const a = document.activeElement; return !!a && (a.matches("input, textarea, select") || a.isContentEditable); };
  // Match the page, not the phone's setting: Concierge is always dark, Brief follows the phone.
  const light = () => {
    const [r, g, b] = (getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [20, 20, 20]).map(Number);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 140;
  };
  // Scrolling down tucks it away; scrolling up (or reaching the top) brings it back.
  let tucked = false;
  const update = () => {
    root.classList.toggle("appbar-light", light());
    const on = !(touch && typing()), h = `${nav.offsetHeight || 60}px`;
    root.classList.toggle("appbar-off", !on);
    if (!on || tucked) openMore(false);
    root.classList.toggle("appbar-tucked", on && tucked);
    root.style.setProperty("--appbar-room", on && !tucked ? h : "0px");  // page end stays clear of it
    root.style.setProperty("--appbar", on && !tucked ? h : "0px");  // what fixed things sit on
  };
  // Only his own scrolling counts: a page jumping to its newest message on open (Inbox) must not hide it.
  let touched = 0;
  for (const ev of ["touchmove", "wheel", "keydown"]) addEventListener(ev, () => { touched = Date.now(); }, {capture: true, passive: true});
  const lastY = new WeakMap();
  document.addEventListener("scroll", e => {
    const el = e.target === document ? document.scrollingElement : e.target;
    if (!el || el.scrollHeight - el.clientHeight < 200) return;  // a short list isn't worth hiding for
    const y = el.scrollTop, prev = lastY.has(el) ? lastY.get(el) : 0;
    lastY.set(el, y);
    if (Date.now() - touched > 800) return;
    if (Math.abs(y - prev) < 6) return;
    const want = y > prev && y > 60;
    if (want !== tucked) { tucked = want; update(); }
  }, {capture: true, passive: true});
  // Installing from any pane installs the one app, never a separate copy of the pane.
  let link = document.querySelector('link[rel="manifest"]');
  if (!link) { link = document.createElement("link"); link.rel = "manifest"; document.head.append(link); }
  link.href = "/claude/app/manifest.webmanifest";
  document.head.append(css);
  document.body.append(scrim, sheet, nav);
  addEventListener("resize", update);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", update);
  document.addEventListener("focusin", update);
  document.addEventListener("focusout", () => setTimeout(update, 50));
  update();
  // Offline: sw.js keeps the last copy of every pane and holds edits made with no signal. This pill says
  // what's waiting, what was refused when it finally went, and what was sent in the last day; tap it for
  // the list. ✕ clears the sent and refused ones. Notifications (if he turns them on) say when they go.
  if (!("serviceWorker" in navigator) || !window.caches) return;
  navigator.serviceWorker.register("/claude/sw.js", {scope: "/"}).catch(() => {});
  const pill = document.createElement("div");
  pill.id = "appbar-outbox";
  pill.hidden = true;
  const pillCss = document.createElement("style");
  pillCss.textContent = `
    #appbar-outbox { position:fixed; left:8px; right:8px; bottom:calc(var(--appbar, 0px) + 8px); z-index:2147482999;
      max-height:60vh; overflow:auto; border-radius:14px; background:#2a2416; color:#f3d9a4; border:1px solid #5a4a22;
      font:14px/1.35 system-ui, -apple-system, sans-serif; box-shadow:0 6px 24px rgba(0,0,0,.35) }
    #appbar-outbox[hidden] { display:none }
    #appbar-outbox .top { display:flex; align-items:center; gap:8px; padding:10px 12px }
    #appbar-outbox .top > button:first-child { all:unset; flex:1; cursor:pointer; min-height:28px }
    #appbar-outbox .drop { all:unset; cursor:pointer; float:right; padding:2px 10px; border-radius:8px; background:#4a3d1f }
    #appbar-outbox .x { all:unset; cursor:pointer; padding:4px 8px; font-size:16px }
    #appbar-outbox ul { list-style:none; margin:0; padding:0 12px 10px }
    #appbar-outbox li { padding:8px 0; border-top:1px solid #4a3d1f }
    #appbar-outbox small { display:block; color:#c9b58a }
    #appbar-outbox .bell { all:unset; cursor:pointer; margin:0 12px 12px; padding:8px 12px; border-radius:10px; background:#4a3d1f }`;
  document.head.append(pillCss);
  document.body.append(pill);
  let open = false;
  const esc = t => String(t ?? "").replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
  const when = iso => new Date(iso).toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit"});
  const outbox = async () => {
    const box = await caches.open("outbox"), keys = await box.keys();
    const items = await Promise.all(keys.map(k => box.match(k).then(r => r.json()).then(i => ({...i, key: k,
      kind: k.url.includes("/outbox/failed-") ? "failed" : k.url.includes("/outbox/sent-") ? "sent" : "waiting"}))));
    const of = kind => items.filter(i => i.kind === kind);
    const [waiting, failed, sent] = [of("waiting"), of("failed"), of("sent")];
    const summary = [waiting.length && `⏳ ${waiting.length} waiting for signal`, failed.length && `⚠ ${failed.length} not sent`,
      sent.length && `✓ ${sent.length} sent`].filter(Boolean).join(" · ");
    pill.hidden = !items.length;
    if (!items.length) return;
    // A waiting thread answer or message can be taken back before it ever leaves the phone.
    const drop = i => i.kind === "waiting" && /\/threads\/\d+\/(answer|say)$/.test(i.url)
      ? ` <button type="button" class="drop" data-k="${esc(i.key.url)}">Take back</button>` : "";
    const row = i => `<li>${i.kind === "waiting" ? "⏳" : i.kind === "failed" ? "⚠" : "✓"} ${esc(i.label || "An edit")}${drop(i)}<small>${
      i.kind === "waiting" ? `made ${when(i.at)} with no signal, sends when you're back online`
      : i.kind === "failed" ? `not sent: ${esc(i.error)}` : `made ${when(i.at)} offline, sent ${when(i.done)}`}</small></li>`;
    const bell = waiting.length && window.Notification?.permission === "default";
    pill.innerHTML = `<div class="top"><button type="button" aria-expanded="${open}">${summary} <span aria-hidden="true">${open ? "▾" : "▸"}</span></button>` +
      (failed.length || sent.length ? `<button type="button" class="x" aria-label="Clear sent and not-sent">✕</button>` : "") + `</div>` +
      (open ? `<ul>${[...waiting, ...failed, ...sent].map(row).join("")}</ul>` +
        (bell ? `<button type="button" class="bell">🔔 Tell me when they're sent</button>` : "") : "");
    pill.querySelector(".top > button").addEventListener("click", () => { open = !open; outbox(); });
    pill.querySelector(".x")?.addEventListener("click", async () => {
      await Promise.all(items.filter(i => i.kind !== "waiting").map(i => box.delete(i.key))); outbox();
    });
    pill.querySelectorAll(".drop").forEach(b => b.addEventListener("click", async () => {
      await box.delete(b.dataset.k); dispatchEvent(new Event("outbox-changed"));
    }));
    pill.querySelector(".bell")?.addEventListener("click", () => Notification.requestPermission().then(outbox));
  };
  const flush = () => { navigator.serviceWorker.controller?.postMessage("flush"); outbox(); };
  navigator.serviceWorker.addEventListener("message", e => { if (e.data === "outbox") outbox(); });
  addEventListener("online", flush);
  addEventListener("offline", outbox);
  addEventListener("outbox-changed", outbox);
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && flush());
  flush();
})();
