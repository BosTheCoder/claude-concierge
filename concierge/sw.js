// The Concierge app offline. Served at /claude/sw.js with Service-Worker-Allowed: / and registered
// by appbar.js, so it covers every pane on the tailnet's main address.
// Reads: network first, last good copy when there's no network (a page waits at most 4 s for it).
// Edits: a Tasks edit or a thread answer/message made offline is kept in the "outbox" cache and sent
// when the network is back (Background Sync, or the next time a pane opens). appbar.js shows the count.
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
  const box = await caches.open(OUTBOX);
  await box.put(`/outbox/${Date.now()}-${Math.random().toString(36).slice(2)}`,
    new Response(JSON.stringify({url: req.url, body, at: new Date().toISOString()})));
  try { await self.registration.sync.register("outbox"); } catch {}
  return new Response(JSON.stringify({ok: true, queued: true}), {status: 202, headers: {"Content-Type": "application/json"}});
}

self.addEventListener("sync", e => { if (e.tag === "outbox") e.waitUntil(flush(true)); });
self.addEventListener("message", e => { if (e.data === "flush") e.waitUntil(flush()); });

// Oldest first. Edits to one task chain: each carries the etag its page saw, so once an earlier one
// lands, a later one based on the same copy takes the new etag instead of being refused as stale.
// A refusal (4xx: TickTick changed it elsewhere, a bad request) is kept as failed for appbar.js to show.
let flushing = null;
function flush(strict) {
  return flushing ??= (async () => {
    const box = await caches.open(OUTBOX), etags = {};
    for (const key of await box.keys()) {
      if (key.url.includes("/outbox/failed-")) continue;
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
      if (!r.ok) await box.put(key.url.replace("/outbox/", "/outbox/failed-"),
        new Response(JSON.stringify({...item, error: reply.error || `error ${r.status}`})));
      await box.delete(key);
    }
  })().finally(() => { flushing = null; });
}
