type Scope = 'external' | 'mfu';
type UnreadCounts = { total: number; notifications: number; chat: number };
type SocketLike = {
  on: (event: string, callback: (payload?: any) => void) => void;
  disconnect: () => void;
};

declare global {
  interface Window {
    io?: (options?: Record<string, unknown>) => SocketLike;
  }
}

let socket: SocketLike | null = null;
let fallbackTimer: number | null = null;

export function stopNotificationRealtime() {
  if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
  fallbackTimer = null;
  socket?.disconnect();
  socket = null;
}

export function startNotificationRealtime(
  scope: Scope,
  refresh: () => Promise<void>,
  apply: (counts: UnreadCounts) => void,
) {
  stopNotificationRealtime();
  const safeRefresh = () => { void refresh().catch(() => undefined); };

  const startFallback = () => {
    if (fallbackTimer !== null) return;
    fallbackTimer = window.setInterval(safeRefresh, 30_000);
  };
  const stopFallback = () => {
    if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
    fallbackTimer = null;
  };

  if (!window.io) {
    startFallback();
    return;
  }

  socket = window.io({ path: '/socket.io', transports: ['websocket', 'polling'] });
  socket.on('connect', () => { stopFallback(); safeRefresh(); });
  socket.on('disconnect', startFallback);
  socket.on('connect_error', startFallback);
  socket.on('notif_unread', (payload: any = {}) => {
    const payloadScope = payload.scope || 'external';
    if (scope !== payloadScope) return;
    const notifications = Math.max(0, Number(payload.notifications || 0));
    const chat = Math.max(0, Number(payload.chat || 0));
    apply({
      notifications,
      chat,
      total: Math.max(0, Number(payload.total ?? payload.count ?? (notifications + chat))),
    });
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) safeRefresh();
  });
}
