// The bottom bar of his one phone app (Concierge): every productivity page on the tailnet's main
// address is a pane of it. A page joins with one line: <script src="/claude/appbar.js" defer></script>
// That also points the page at the app's manifest, so installing from any pane installs the one app.
// Fixed-bottom things on a page sit above the bar with bottom: calc(var(--appbar, 0px) + ...).
(() => {
  if (location.port || document.getElementById("appbar")) return;  // :18787 etc. are other addresses
  const PANES = [
    ["/claude/app", "✦", "Concierge"], ["/claude/tasks", "☑", "Tasks"], ["/brief/", "☀", "Brief"],
    ["/money/", "£", "Money"], ["/inbox/", "✉", "Inbox"],
    // The vault browser's viewer, proxied by the concierge so it opens inside the app.
    // Its controls (restart, tabs) are on the Apps page and the icon's long-press menu.
    ["/claude/vault/", "◳", "Browser"],
    ["/", "⋯", "Apps"],  // the tailnet home page: every app he has
  ];
  const wanted = () => matchMedia("(display-mode: standalone)").matches || innerWidth < 700;
  const trim = s => s.replace(/\/$/, ""), path = trim(location.pathname);
  const here = PANES.find(([p]) => p === "/" ? path === "" : p.startsWith("/") && (path === trim(p) || path.startsWith(trim(p) + "/")));
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
    body { padding-bottom:var(--appbar-room, 0px) !important }`;
  const nav = document.createElement("nav");
  nav.id = "appbar";
  nav.setAttribute("aria-label", "Apps");
  nav.innerHTML = PANES.map(([href, icon, name]) =>
    `<a href="${href}"${here && here[0] === href ? ' aria-current="page"' : ""}><b aria-hidden="true">${icon}</b>${name}</a>`).join("");
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
    const on = wanted() && !typing(), h = `${nav.offsetHeight || 60}px`;
    root.classList.toggle("appbar-off", !on);
    root.classList.toggle("appbar-tucked", on && tucked);
    root.style.setProperty("--appbar-room", on ? h : "0px");  // page end stays clear of it
    root.style.setProperty("--appbar", on && !tucked ? h : "0px");  // what fixed things sit on
  };
  const lastY = new WeakMap();
  document.addEventListener("scroll", e => {
    const el = e.target === document ? document.scrollingElement : e.target;
    if (!el || el.scrollHeight - el.clientHeight < 200) return;  // a short list isn't worth hiding for
    const y = el.scrollTop, prev = lastY.has(el) ? lastY.get(el) : 0;
    lastY.set(el, y);
    if (Math.abs(y - prev) < 6) return;
    const want = y > prev && y > 60;
    if (want !== tucked) { tucked = want; update(); }
  }, {capture: true, passive: true});
  // Installing from any pane installs the one app, never a separate copy of the pane.
  let link = document.querySelector('link[rel="manifest"]');
  if (!link) { link = document.createElement("link"); link.rel = "manifest"; document.head.append(link); }
  link.href = "/claude/app/manifest.webmanifest";
  document.head.append(css);
  document.body.append(nav);
  addEventListener("resize", update);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", update);
  document.addEventListener("focusin", update);
  document.addEventListener("focusout", () => setTimeout(update, 50));
  update();
})();
