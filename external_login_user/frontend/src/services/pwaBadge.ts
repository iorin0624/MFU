export type NotificationScope = 'external' | 'mfu';

type BadgeNavigator = Navigator & {
  setAppBadge?: (count?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

export function badgeApiUrl(scope: NotificationScope): string {
  return scope === 'mfu'
    ? '/api/mfu-notifications/unread-count'
    : '/external-login/api/notifications/unread-count';
}

function normalizedBadgeCount(rawCount: number): number {
  const count = Math.floor(Number(rawCount));
  return Number.isFinite(count) && count > 0 ? count : 0;
}

async function updateWindowBadge(count: number): Promise<void> {
  const badgeNavigator = navigator as BadgeNavigator;
  try {
    if (count > 0 && typeof badgeNavigator.setAppBadge === 'function') {
      await badgeNavigator.setAppBadge(count);
    } else if (count <= 0 && typeof badgeNavigator.clearAppBadge === 'function') {
      await badgeNavigator.clearAppBadge();
    }
  } catch (reason) {
    console.debug('[badge] window badge update failed', reason);
  }
}

async function updateServiceWorkerBadge(count: number, scope: NotificationScope): Promise<void> {
  if (!('serviceWorker' in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.getRegistration('/');
    const worker = navigator.serviceWorker.controller || registration?.active || registration?.waiting;
    worker?.postMessage({
      type: 'SYNC_BADGE',
      badgeApiUrl: badgeApiUrl(scope),
      count,
    });
  } catch (reason) {
    console.debug('[badge] service worker badge update failed', reason);
  }
}

export async function updatePwaBadge(rawCount: number, scope: NotificationScope): Promise<void> {
  const count = normalizedBadgeCount(rawCount);
  await Promise.all([
    updateWindowBadge(count),
    updateServiceWorkerBadge(count, scope),
  ]);
}
