const SW_VERSION = '2026-03-04-01';
const BADGE_SYNC_MESSAGE = 'SYNC_BADGE';

function badgeApiUrl() {
  return new URL('/external-login/api/notifications/unread-count', self.location.origin).toString();
}

async function setBadgeSafe(count) {
  const normalized = Number.isFinite(Number(count)) ? Math.floor(Number(count)) : 0;
  if (normalized <= 0) {
    await clearBadgeSafe();
    return;
  }
  try {
    if (self.registration && typeof self.registration.setAppBadge === 'function') {
      await self.registration.setAppBadge(normalized);
    }
  } catch (e) {
    console.debug('[sw] set badge failed', e);
  }
}

async function clearBadgeSafe() {
  try {
    if (self.registration && typeof self.registration.clearAppBadge === 'function') {
      await self.registration.clearAppBadge();
    }
  } catch (e) {
    console.debug('[sw] clear badge failed', e);
  }
}

async function syncBadgeFromApi() {
  try {
    const res = await fetch(badgeApiUrl(), {
      method: 'GET',
      credentials: 'include',
      cache: 'no-store',
    });
    if (res.status === 401) {
      await clearBadgeSafe();
      return;
    }
    if (!res.ok) {
      return;
    }
    const data = await res.json().catch(() => ({}));
    const count = Number(data?.count ?? 0);
    if (Number.isFinite(count) && count > 0) {
      await setBadgeSafe(count);
      return;
    }
    await clearBadgeSafe();
  } catch (e) {
    console.debug('[sw] sync badge failed', e);
  }
}

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await clients.claim();
    await syncBadgeFromApi();
  })());
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

  event.waitUntil(Promise.all([showPromise, broadcastPromise, syncBadgeFromApi()]));
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
    await syncBadgeFromApi();
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

self.addEventListener('message', (event) => {
  if (event?.data?.type !== BADGE_SYNC_MESSAGE) return;
  event.waitUntil(syncBadgeFromApi());
});
