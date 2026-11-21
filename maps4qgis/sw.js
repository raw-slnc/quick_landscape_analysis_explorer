const CACHE_NAME = 'maps4qgis-0.1.0-r2';
const APP_SHELL_ASSETS = [
  './index.html',
  './app-en.html',
  './app-ja.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './WMMHR.COF',
  './wmmhr.js',
  './leaflet.min.css',
  './leaflet.min.js',
  './pdf.mjs',
  './pdf.worker.mjs',
  './proj4.js',
  './datums.json'
];

self.addEventListener('install', e => {
  e.waitUntil(
    // ブラウザの保存分ではなくサーバーから取り直して格納する
    caches.open(CACHE_NAME).then(cache => cache.addAll(
      APP_SHELL_ASSETS.map(asset => new Request(asset, { cache: 'reload' }))
    ))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      // 同じサイトの他のアプリのキャッシュは消さない
      Promise.all(keys
        .filter(k => k !== CACHE_NAME && k.startsWith('maps4qgis-'))
        .map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isNetworkFirstAsset(requestUrl) {
  const url = new URL(requestUrl);
  if (url.origin !== self.location.origin) return false;
  return url.pathname.endsWith('/index.html')
    || url.pathname.endsWith('/manifest.json')
    || url.pathname.endsWith('.js')
    || url.pathname.endsWith('.mjs')
    || url.pathname.endsWith('.css')
    || url.pathname.endsWith('.json');
}

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  // 地理院の標高タイルなど他サイトへの通信は素通しする（取込時だけ使い、キャッシュに溜めない）
  if (new URL(e.request.url).origin !== self.location.origin) return;

  // index.html はネットワーク優先、失敗時はキャッシュにフォールバック
  // cache: 'no-cache' でブラウザの保存分を使わず、必ずサーバーに更新を確認する
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' }).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match(e.request).then(cached => cached || caches.match('./index.html')))
    );
    return;
  }

  if (isNetworkFirstAsset(e.request.url)) {
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' }).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // その他のアセットはキャッシュ優先
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (!res || res.status !== 200) return res;
        const clone = res.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        return res;
      }).catch(() => Response.error());
    })
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
