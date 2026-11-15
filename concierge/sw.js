// The Concierge app offline. Served at /claude/sw.js with Service-Worker-Allowed: / and registered
// by appbar.js, so it covers every pane on the tailnet's main address.
// Reads: network first, last good copy when there's no network (a page waits at most 4 s for it).
// Edits: a Tasks edit or a thread answer/message made offline is kept in the "outbox" cache and sent
// when the network is back (Background Sync, or the next time a pane opens). appbar.js lists them.
const PAGES = "concierge-pages-v1", OUTBOX = "outbox";
const PANES = ["/claude/", "/brief/", "/money/", "/inbox/"];  // not /claude/vault: a live screen, nothing to keep
const QUEUED = /^\/claude\/api\/(tt\/update|threads\/[^/]+\/(answer|say))$/;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

const ours = url => url.origin === location.origin && !url.pathname.startsWith("/claude/vault")
  && (url.pathname === "/" || PANES.some(p => url.pathname.startsWith(p)));

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (!ours(url)) return;
  if (req.method === "POST" && QUEUED.test(url.pathname)) return e.respondWith(sendOrQueue(req));
  if (req.method !== "GET" || req.headers.get("Accept")?.includes("text/event-stream") || req.headers.has("Range")) return;
  // Tasks keeps its own last copy, with offline edits applied: an older cached list would undo them on screen.
  if (url.pathname.startsWith("/claude/api/tt/")) return;
  e.respondWith(read(req, e));
});

async function read(req, e) {
  const net = fetch(req).then(r => {
    if (r.ok && r.status === 200) { const copy = r.clone(); e.waitUntil(caches.open(PAGES).then(c => c.put(req, copy))); }
    e.waitUntil(flush());  // the network is back: send anything waiting
    return r;
  });
  const old = () => caches.match(req, {ignoreVary: true});
  if (req.mode !== "navigate") return net.catch(async () => (await old()) || Response.error());
  // A page on a weak signal: show the last copy after 4 s (or as soon as the network fails), not a blank screen.
  const late = Promise.race([new Promise(ok => setTimeout(ok, 4000)), net.catch(() => {})]).then(old);
  return Promise.any([net, late.then(r => r || Promise.reject())]).catch(async () => (await old()) || net);
}

async function sendOrQueue(req) {
  const body = await req.clone().text();
  try { return await fetch(req); } catch {}  // no network: keep it for later
  const label = decodeURIComponent(req.headers.get("X-Outbox-Label") || "") || "An edit";
  const box = await caches.open(OUTBOX);
  await box.put(`/outbox/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    new Response(JSON.stringify({url: req.url, body, label, page: req.referrer, at: new Date().toISOString()})));
  try { await self.registration.sync.register("outbox"); } catch {}
  tell();
  return new Response(JSON.stringify({ok: true, queued: true}), {status: 202, headers: {"Content-Type": "application/json"}});
}

// Every open pane redraws its outbox pill (appbar.js) at once.
const tell = () => self.clients.matchAll({includeUncontrolled: true}).then(cs => cs.forEach(c => c.postMessage("outbox")));
const waiting = url => !/\/outbox\/(failed|sent)-/.test(url);
const DAY = 24 * 3600 * 1000;

self.addEventListener("sync", e => { if (e.tag === "outbox") e.waitUntil(flush(true)); });
self.addEventListener("message", e => { if (e.data === "flush") e.waitUntil(flush()); });

// Oldest first. Edits to one task chain: each carries the etag its page saw, so once an earlier one
// lands, a later one based on the same copy takes the new etag instead of being refused as stale.
// Each one ends as sent (kept a day so the pill can list it) or failed (a 4xx: TickTick changed it
// elsewhere, a bad request) until he clears it. A notification says how it went, if he allowed them.
let flushing = null;
function flush(strict) {
  return flushing ??= (async () => {
    const box = await caches.open(OUTBOX), etags = {}, sent = [], failed = [];
    try {
      for (const key of await box.keys()) {
        if (key.url.includes("/outbox/sent-") && Date.now() - +key.url.split("/outbox/sent-")[1].split("-")[0] > DAY) await box.delete(key);
        if (!waiting(key.url)) continue;
        const item = await (await box.match(key)).json();
        let body = item.body;
        const j = JSON.parse(body || "{}");
        if (j.etag && etags[j.id]?.[j.etag]) body = JSON.stringify({...j, etag: etags[j.id][j.etag]});
        let r;
        try { r = await fetch(item.url, {method: "POST", headers: {"Content-Type": "application/json"}, body}); }
        catch (err) { if (strict) throw err; return; }  // still offline: Background Sync tries again
        if (r.status >= 500) return;  // the server end is down (or TickTick is): keep it, try next time
        const reply = await r.json().catch(() => ({}));
        if (r.ok && reply.task?.etag && j.etag) (etags[j.id] ??= {})[j.etag] = reply.task.etag;
        const done = {...item, done: new Date().toISOString(), ...(!r.ok && {error: reply.error || `error ${r.status}`})};
        await box.put(`/outbox/${r.ok ? "sent" : "failed"}-${Date.now()}-${key.url.split("/outbox/")[1]}`, new Response(JSON.stringify(done)));
        await box.delete(key);
        (r.ok ? sent : failed).push(done);
      }
    } finally {
      if (sent.length || failed.length) { tell(); await announce(sent, failed); }
    }
  })().finally(() => { flushing = null; });
}

async function announce(sent, failed) {
  if (self.Notification?.permission !== "granted") return;
  const n = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
  const title = [sent.length && `Sent ${n(sent.length, "edit")} made offline`, failed.length && `${n(failed.length, "edit")} not sent`]
    .filter(Boolean).join(" · ");
  const body = [...failed.map(f => `✕ ${f.label}: ${f.error}`), ...sent.map(s => `✓ ${s.label}`)].join("\n");
  const page = (failed[0] || sent[sent.length - 1]).page || "/claude/app";
  await self.registration.showNotification(title, {body, tag: "outbox", icon: "/claude/icon-512.png", data: {page}});
}

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const page = e.notification.data?.page || "/claude/app";
  e.waitUntil(self.clients.matchAll({type: "window"}).then(cs => cs[0] ? cs[0].navigate(page).then(c => c?.focus()) : self.clients.openWindow(page)));
});
