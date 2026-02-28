self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_e) {
    payload = {};
  }

  const title = payload.title || 'MFU Chat';
  const body = payload.body || '新着メッセージがあります';
  const url = payload.url || '/chat/';

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      data: { url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = event.notification?.data?.url || '/chat/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ('focus' in client) {
          if (client.url.includes(targetUrl)) {
            return client.focus();
          }
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
      return null;
    })
  );
});
