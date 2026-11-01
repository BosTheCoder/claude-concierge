// The Claude button. Any page on the tailnet host loads this as /claude/claude.js and calls
//   claudeAsk({title, cwd, context, placeholder})
// from its own ✦ button. The sheet takes an optional note and starts a concierge job: a live
// Claude Code chat in `cwd` that already has `context` (a string, or a function read at tap time).
// The sheet closes as soon as the job exists; a tray in the corner lists the day's chats and
// turns each into an "Open" link once its URL is in (GET /claude/api/jobs).
// Spec: docs/superpowers/specs/2026-10-30-claude-button-design.md
(() => {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  // The page's own colours when it has them (the brief does), plain dark/light otherwise.
  const css = `
  .cl-sheet { width:min(560px, calc(100vw - 32px)); max-height:calc(100dvh - 32px); border:1px solid var(--line, #8884);
    border-radius:16px; padding:16px; background:var(--card, Canvas); color:var(--ink, CanvasText);
    font:16px/1.4 system-ui, sans-serif; color-scheme:light dark; box-sizing:border-box }
  .cl-sheet::backdrop { background:#0008 }
  .cl-sheet h2 { margin:0 0 4px; font-size:17px; font-weight:700; text-transform:none; letter-spacing:normal; color:inherit }
  .cl-sheet .cl-t { margin:0 0 12px; color:var(--mute, GrayText); font-size:14px; overflow-wrap:anywhere }
  .cl-sheet textarea { width:100%; box-sizing:border-box; min-height:84px; padding:10px 12px; border:1px solid var(--line, #8884);
    border-radius:12px; background:var(--bg, Field); color:inherit; font:inherit; resize:vertical }
  .cl-sheet .cl-msg { margin:10px 0 0; font-size:15px; overflow-wrap:anywhere }
  .cl-sheet .cl-msg:empty { display:none }
  .cl-sheet .cl-row { display:flex; justify-content:flex-end; gap:8px; margin-top:12px }
  .cl-sheet button, .cl-sheet .cl-open { min-height:44px; padding:0 18px; border-radius:22px; font:inherit; font-weight:600;
    cursor:pointer; display:inline-flex; align-items:center; text-decoration:none }
  .cl-sheet .cl-cancel { border:1px solid var(--line, #8884); background:none; color:inherit }
  .cl-sheet .cl-go, .cl-sheet .cl-open { border:0; background:var(--blue, LinkText); color:var(--bg, Canvas) }
  .cl-sheet .cl-go:disabled { opacity:.5 }
  .cl-tray { position:fixed; right:16px; bottom:16px; z-index:50; width:min(300px, calc(100vw - 32px));
    border:1px solid var(--line, #8884); border-radius:14px; background:var(--card, Canvas); color:var(--ink, CanvasText);
    font:14px/1.35 system-ui, sans-serif; color-scheme:light dark; box-shadow:0 6px 24px #0006; box-sizing:border-box }
  .cl-tray[hidden] { display:none }
  .cl-tray > button { all:unset; box-sizing:border-box; display:flex; width:100%; min-height:44px; padding:0 14px;
    align-items:center; justify-content:space-between; font-weight:700; cursor:pointer }
  .cl-tray ul { list-style:none; margin:0; padding:0 6px 6px; max-height:50dvh; overflow:auto }
  .cl-tray.cl-shut ul { display:none }
  .cl-tray li a, .cl-tray li > span { display:block; padding:8px; border-radius:10px; color:inherit; text-decoration:none }
  .cl-tray li a:hover { background:var(--line, #8882) }
  .cl-tray .cl-n { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
  .cl-tray .cl-s { display:block; font-size:12px; color:var(--mute, GrayText); overflow-wrap:anywhere }
  .cl-tray a .cl-s { color:var(--blue, LinkText); font-weight:600 }
  .cl-tray .cl-bad .cl-s { color:var(--red, #e5484d) }`;
  let dlg, opts, busy = false;
  const $ = s => dlg.querySelector(s);

  function build() {
    const st = document.createElement("style");
    st.textContent = css;
    document.head.append(st);
    dlg = document.createElement("dialog");
    dlg.className = "cl-sheet";
    dlg.setAttribute("aria-label", "Ask Claude");
    dlg.innerHTML = `<form><h2>✦ Ask Claude</h2><p class="cl-t"></p>
      <textarea name="note" aria-label="Note for Claude (optional)"></textarea>
      <p class="cl-msg" role="status" aria-live="polite"></p>
      <div class="cl-row"><button type="button" class="cl-cancel">Cancel</button><button class="cl-go">Go</button></div></form>`;
    document.body.append(dlg);
    $(".cl-cancel").onclick = () => { if (!busy) dlg.close(); };
    $("form").onsubmit = e => { e.preventDefault(); go(); };
    dlg.addEventListener("cancel", e => { if (busy) e.preventDefault(); });
  }

  async function go() {
    if (busy) return;
    let context;
    try { context = String((typeof opts.context === "function" ? opts.context() : opts.context) ?? ""); }
    catch (e) { context = `(the page couldn't build its context: ${e.message})`; }
    busy = true;
    $(".cl-go").disabled = true;
    $(".cl-msg").textContent = "Starting…";
    try {
      const r = await fetch("/claude/api/spawn", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: String(opts.title || "Untitled").slice(0, 120), cwd: opts.cwd || "",
          context: context.slice(0, 30000), note: $("textarea").value.trim().slice(0, 4000), page: location.href})});
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
      dlg.close();
      tray.open(true);
      tray.refresh();
    } catch (e) {
      $(".cl-msg").textContent = `Didn't start: ${e.message}`;
      $(".cl-go").disabled = false;
    }
    busy = false;
  }

  // The tray: today's chats, newest first. Polls fast only while one is still starting.
  const tray = (() => {
    let el, timer, shut;
    try { shut = localStorage.getItem("cl-tray-shut"); } catch { shut = null; }
    shut = shut === null ? !matchMedia("(min-width: 1100px)").matches : shut === "1";
    function build() {
      const st = document.createElement("style");
      st.textContent = css;
      if (!dlg) document.head.append(st);
      el = document.createElement("aside");
      el.className = "cl-tray";
      el.hidden = true;
      el.setAttribute("aria-label", "Claude chats");
      el.innerHTML = `<button type="button" aria-expanded="true"><span>✦ Chats</span><span class="cl-c"></span></button><ul></ul>`;
      el.querySelector("button").onclick = () => open(el.classList.contains("cl-shut"));
      document.body.append(el);
      open(!shut, false);
    }
    function open(yes, save = true) {
      if (!el) build();
      el.classList.toggle("cl-shut", !yes);
      el.querySelector("button").setAttribute("aria-expanded", String(yes));
      if (save) try { localStorage.setItem("cl-tray-shut", yes ? "0" : "1"); } catch {}
    }
    function row(j) {
      const name = `<span class="cl-n">[${esc(j.id)}] ${esc(j.title)}</span>`;
      if (j.status === "failed") return `<li class="cl-bad"><span>${name}<span class="cl-s">Failed: ${esc(j.error || "see Telegram")}</span></span></li>`;
      if (/^https:\/\//.test(j.rc_url || ""))
        return `<li><a href="${esc(j.rc_url)}" target="_blank" rel="noopener">${name}<span class="cl-s">Open chat ›${j.status === "running" ? "" : " · " + esc(j.status)}</span></a></li>`;
      return `<li><span>${name}<span class="cl-s">${j.status === "running" ? "Starting…" : esc(j.status)}</span></span></li>`;
    }
    async function refresh() {
      clearTimeout(timer);
      let jobs = [];
      try { jobs = (await (await fetch("/claude/api/jobs")).json()).jobs || []; } catch {}
      if (!el) build();
      el.hidden = !jobs.length;
      el.querySelector(".cl-c").textContent = jobs.length;
      el.querySelector("ul").innerHTML = jobs.map(row).join("");
      const starting = jobs.some(j => j.status === "running" && !j.rc_url);
      timer = setTimeout(refresh, starting ? 2000 : 30000);
    }
    return {open, refresh};
  })();
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", tray.refresh); else tray.refresh();

  window.claudeAsk = o => {
    if (busy) { if (!dlg.open) dlg.showModal(); return; }  // one start at a time (it takes a second now)
    if (!dlg) build();
    opts = o || {};
    $(".cl-t").textContent = opts.title || "";
    $("textarea").value = "";
    $("textarea").placeholder = opts.placeholder || "What should it do? (optional)";
    $(".cl-msg").textContent = "";
    $(".cl-go").hidden = false;
    $(".cl-go").disabled = false;
    $(".cl-cancel").textContent = "Cancel";
    dlg.showModal();
    $("textarea").focus();
  };
})();
