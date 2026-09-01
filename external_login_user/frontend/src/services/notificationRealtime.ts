type Scope = 'external' | 'mfu';
type UnreadCounts = { total: number; notifications: number; chat: number };
import { onPortalConnection, onPortalEvent, onPortalResume, portalSocket } from '@/services/portalRealtime';

let fallbackTimer: number | null = null;
let cleanup: Array<() => void> = [];

export function stopNotificationRealtime() {
  if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
  fallbackTimer = null;
  cleanup.forEach((dispose) => dispose());
  cleanup = [];
}

export function startNotificationRealtime(
  scope: Scope,
  refresh: () => Promise<void>,
  apply: (counts: UnreadCounts) => void,
) {
  stopNotificationRealtime();
  const safeRefresh = () => { if (!document.hidden) void refresh().catch(() => undefined); };
  cleanup.push(onPortalResume(safeRefresh));

  const startFallback = () => {
    if (fallbackTimer !== null) return;
    fallbackTimer = window.setInterval(safeRefresh, 30_000);
  };
  const stopFallback = () => {
    if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
    fallbackTimer = null;
  };

  if (!portalSocket()) {
    startFallback();
    return;
  }

  cleanup.push(onPortalConnection((connected) => {
    if (connected) { stopFallback(); safeRefresh(); }
    else startFallback();
  }));
  cleanup.push(onPortalEvent('notif_unread', (payload: any = {}) => {
    const payloadScope = payload.scope || 'external';
    if (scope !== payloadScope) return;
    const notifications = Math.max(0, Number(payload.notifications || 0));
    const chat = Math.max(0, Number(payload.chat || 0));
    apply({
      notifications,
      chat,
      total: Math.max(0, Number(payload.total ?? payload.count ?? (notifications + chat))),
    });
  }));
}
