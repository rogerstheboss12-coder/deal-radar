// Deal Radar service worker: makes the page installable and shows push
// notifications. It caches only the page shell (a few KB); deal data always
// comes fresh from the network.
const SHELL = "dr-shell-v1";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(["./", "manifest.webmanifest", "icon-192.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.mode === "navigate" && url.origin === location.origin) {
    // Network first so updates show immediately; fall back to the cached shell offline.
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(SHELL).then(c => c.put("./", copy)); return r;
    }).catch(() => caches.match("./")));
  }
});
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: "Deal Radar", body: e.data?.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Deal Radar", {
    body: d.body || "", icon: d.icon || "icon-192.png", badge: "icon-192.png",
    tag: d.tag, data: { url: d.url || "./" }
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data?.url || "./"));
});
