// App shell offline: the page, script, styles and fonts come from the cache when there is no reception.
// Data never comes from here: the app keeps its own copy in IndexedDB and queues entries until it is online.
// The portal fills in the Kennung and the file list (incl. the installation's branding).
const SHELL = "__KENNUNG__-shell-v9";
const FILES = __FILES__;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/app/api/")) return;
  // network first, so a new version arrives as soon as there is reception
  e.respondWith(fetch(e.request).then((resp) => {
    if (resp.ok && FILES.includes(url.pathname)) {
      const copy = resp.clone();
      caches.open(SHELL).then((c) => c.put(e.request, copy));
    }
    return resp;
  }).catch(() => caches.match(e.request, {ignoreSearch: true}).then((hit) => hit || caches.match("/app/"))));
});
