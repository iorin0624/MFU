const CACHE = 'mfu-chat-v1';
const STATIC_ASSETS = [
  '/chat/',
  '/chat/manifest.json',
  '/chat/sw.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(STATIC_ASSETS)));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/chat/static/')) {
    event.respondWith(caches.match(event.request).then((res) => res || fetch(event.request)));
    return;
  }
  if (url.pathname.startsWith('/chat/api/')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
  }
});

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : { title: 'MFU Chat', body: '新着通知' };
  event.waitUntil(self.registration.showNotification(data.title || 'MFU Chat', {
    body: data.body || '',
    data,
  }));
});
