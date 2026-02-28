self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_e) {
    payload = {};
  }

  const title = payload.title || 'MFU Chat';
  const body = payload.body || '新着メッセージがあります';
  const url = payload.url || (payload.event_id ? `/chat/events/${payload.event_id}` : '/chat/');

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      data: { url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawTargetUrl = event.notification?.data?.url || '/chat/';
  const targetUrlObj = new URL(rawTargetUrl, self.location.origin);
  const targetUrl = targetUrlObj.toString();

  event.waitUntil((async () => {
    const windowClients = await clients.matchAll({ type: 'window', includeUncontrolled: true });

    const exactClient = windowClients.find((client) => {
      try {
        const u = new URL(client.url);
        return u.origin === targetUrlObj.origin
          && u.pathname === targetUrlObj.pathname
          && u.search === targetUrlObj.search
          && u.hash === targetUrlObj.hash;
      } catch (_e) {
        return false;
      }
    });

    if (exactClient && 'focus' in exactClient) {
      await exactClient.focus();
      return;
    }

    if (clients.openWindow) {
      const openedClient = await clients.openWindow(targetUrl);
      if (openedClient && 'focus' in openedClient) {
        await openedClient.focus();
        return;
      }
    }

    const chatClient = windowClients.find((client) => {
      try {
        const u = new URL(client.url);
        return u.origin === self.location.origin && u.pathname.startsWith('/chat/');
      } catch (_e) {
        return false;
      }
    });

    if (chatClient && 'navigate' in chatClient) {
      await chatClient.navigate(targetUrl);
      if ('focus' in chatClient) {
        await chatClient.focus();
      }
    }
  })());
});
