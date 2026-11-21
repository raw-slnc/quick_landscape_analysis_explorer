const CACHE_NAME = 'fieldmap-probe-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      // 同じサイトの他のアプリのキャッシュは消さない
      Promise.all(keys
        .filter(key => key !== CACHE_NAME && key.startsWith('fieldmap-probe-'))
        .map(key => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;

      if (event.request.mode === 'navigate') {
        return fetch(event.request).catch(() => caches.match('./index.html'));
      }

      return fetch(event.request);
    })
  );
});
