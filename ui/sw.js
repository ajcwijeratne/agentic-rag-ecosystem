/* Command Centre service worker.
   Minimal network-first passthrough — its main job is to make the app
   installable (Chromium wants a fetch handler). It caches the app shell so a
   cold start still renders if the orchestrator is briefly unavailable; live
   API calls always go to the network. */
const CACHE = "cc-shell-v4";
const SHELL = ["/app/command_centre.html", "/app/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never cache API POSTs etc.
  event.respondWith(
    fetch(req)
      .then((res) => {
        // keep the app shell fresh
        if (SHELL.some((p) => req.url.endsWith(p))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
