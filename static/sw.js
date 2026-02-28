self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

function normalizeTargetUrl(rawUrl, eventId) {
  const fallbackPath = eventId ? `/chat/events/${eventId}` : '/';
  const candidate = typeof rawUrl === 'string' && rawUrl.trim() ? rawUrl.trim() : fallbackPath;

  if (candidate.startsWith('//')) {
    return `${self.location.origin}/`;
  }

  try {
    const url = new URL(candidate, self.location.origin);
    if (url.origin !== self.location.origin) {
      return `${self.location.origin}/`;
    }
    return url.toString();
  } catch (_e) {
    return `${self.location.origin}/`;
  }
}

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_e) {
    payload = {};
  }

  const title = payload.title || 'MFU';
  const body = payload.body || '新着通知があります';
  const targetUrl = normalizeTargetUrl(payload.url, payload.event_id);

  const showPromise = self.registration.showNotification(title, {
    body,
    data: { url: targetUrl },
  });

  const broadcastPromise = (async () => {
    try {
      const windowClients = await clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of windowClients) {
        try {
          const origin = new URL(client.url).origin;
          if (origin !== self.location.origin) continue;
          client.postMessage({ type: 'MFU_PUSH_NAV', url: targetUrl, ts: Date.now() });
        } catch (_e) {
          // noop
        }
      }
    } catch (_e) {
      // noop
    }
  })();

  event.waitUntil(Promise.all([showPromise, broadcastPromise]));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  let targetUrl = `${self.location.origin}/`;
  try {
    targetUrl = normalizeTargetUrl(event.notification?.data?.url);
  } catch (_e) {
    targetUrl = `${self.location.origin}/`;
  }

  event.waitUntil((async () => {
    let windowClients = [];
    try {
      windowClients = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    } catch (_e) {
      windowClients = [];
    }

    const sameOriginClient = windowClients.find((client) => {
      try {
        return new URL(client.url).origin === self.location.origin;
      } catch (_e) {
        return false;
      }
    });

    if (sameOriginClient) {
      if ('navigate' in sameOriginClient) {
        try {
          await sameOriginClient.navigate(targetUrl);
        } catch (_e) {
          // fallback to focus/openWindow below
        }
      }
      if ('focus' in sameOriginClient) {
        try {
          await sameOriginClient.focus();
          return;
        } catch (_e) {
          // fallback to openWindow below
        }
      }
    }

    if (clients.openWindow) {
      try {
        await clients.openWindow(targetUrl);
      } catch (_e) {
        // noop
      }
    }
  })());
});
