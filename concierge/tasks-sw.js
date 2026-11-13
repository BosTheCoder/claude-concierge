// Lets Chrome install the tasks page as an app, and opens it with no network at all from the last copy.
// Network first, so a change to the page shows on the next open; the cache is only the fallback.
// Task data is not cached here: the page keeps its own last copy (localStorage) and refreshes it.
const CACHE = "tasks-shell-v1", SHELL = ["tasks", "search.js"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !SHELL.some(p => url.pathname.endsWith("/" + p))) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
