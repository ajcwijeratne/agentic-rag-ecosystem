/* Command Centre service worker.
   Network-first: always fetch the live page from the orchestrator, fall back to
   cache only when offline. Shell requests bypass the browser HTTP cache so a
   new deploy can never be masked by a stale cached page. Bump CACHE to force
   every client to update on its next navigation. */
const CACHE = "cc-shell-v12";
const SHELL = ["/app/command_centre.html", "/app/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never cache API POSTs etc.
  const isShell = req.mode === "navigate" || SHELL.some((p) => req.url.endsWith(p));
  event.respondWith(
    fetch(isShell ? new Request(req.url, { cache: "reload" }) : req)
      .then((res) => {
        if (isShell) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then((m) => m || caches.match("/app/command_centre.html")))
  );
});

/* Phone notifications (Phase 3, 26 Nov 2026): approvals waiting, failures,
   commitments due today. The server sends {title, body, url, tag, kind};
   tapping the notification opens or focuses the Command Centre at url. */
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data ? event.data.text() : "" }; }
  const title = d.title || "Command Centre";
  event.waitUntil(self.registration.showNotification(title, {
    body: d.body || "",
    tag: d.tag || d.kind || "cc",
    renotify: true,
    icon: "/app/icons/icon-192.png",
    badge: "/app/icons/icon-192.png",
    data: { url: d.url || "/app/command_centre.html" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/app/command_centre.html", self.location.origin);
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.startsWith(self.location.origin + "/app/")) {
          return w.navigate(target.href).then((n) => (n || w).focus()).catch(() => w.focus());
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});

