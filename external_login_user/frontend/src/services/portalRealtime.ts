type EventHandler = (payload?: any) => void;

type SocketLike = {
  connected?: boolean;
  connect?: () => void;
  on: (event: string, callback: EventHandler) => void;
  off?: (event: string, callback?: EventHandler) => void;
  emit: (event: string, payload?: any, callback?: (response: any) => void) => void;
  disconnect: () => void;
};

declare global {
  interface Window {
    io?: (options?: Record<string, unknown>) => SocketLike;
  }
}

let socket: SocketLike | null = null;
const listeners = new Map<string, Set<EventHandler>>();
const lifecycleListeners = new Set<(connected: boolean) => void>();
const resumeListeners = new Set<() => void>();
const boundEvents = new Set<string>();
let resumeBound = false;
let resumeTimer = 0;
let chatAuthScope = '';

export function setPortalChatAuthScope(value: string) {
  const next = value === 'mfu' ? 'mfu' : '';
  if (next === chatAuthScope) return;
  chatAuthScope = next;
  if (socket) {
    socket.disconnect();
    socket = null;
    boundEvents.clear();
  }
}

function scheduleResume() {
  if (document.hidden) return;
  window.clearTimeout(resumeTimer);
  resumeTimer = window.setTimeout(() => {
    if (document.hidden) return;
    const active = portalSocket();
    if (active && !active.connected) active.connect?.();
    resumeListeners.forEach((handler) => handler());
  }, 150);
}

export function onPortalResume(handler: () => void): () => void {
  if (!resumeBound) {
    resumeBound = true;
    document.addEventListener('visibilitychange', scheduleResume);
    window.addEventListener('pageshow', scheduleResume);
    window.addEventListener('online', scheduleResume);
    window.addEventListener('focus', scheduleResume);
  }
  resumeListeners.add(handler);
  return () => resumeListeners.delete(handler);
}

function dispatch(event: string, payload?: any) {
  listeners.get(event)?.forEach((handler) => handler(payload));
}

function bind(event: string) {
  if (!socket || boundEvents.has(event)) return;
  boundEvents.add(event);
  socket?.on(event, (payload) => dispatch(event, payload));
}

export function portalSocket(): SocketLike | null {
  if (socket || !window.io) return socket;
  socket = window.io({
    path: '/socket.io',
    transports: ['websocket', 'polling'],
    query: chatAuthScope ? { chat_auth_scope: chatAuthScope } : undefined,
  });
  boundEvents.clear();
  socket.on('connect', () => lifecycleListeners.forEach((handler) => handler(true)));
  socket.on('disconnect', () => lifecycleListeners.forEach((handler) => handler(false)));
  socket.on('connect_error', () => lifecycleListeners.forEach((handler) => handler(false)));
  socket.on('force_logout', (payload: any = {}) => {
    const redirect = String(payload.redirect || '/external-login/');
    socket?.disconnect();
    socket = null;
    boundEvents.clear();
    window.location.replace(redirect);
  });
  listeners.forEach((_handlers, event) => bind(event));
  return socket;
}

export function onPortalEvent(event: string, handler: EventHandler): () => void {
  let handlers = listeners.get(event);
  if (!handlers) {
    handlers = new Set<EventHandler>();
    listeners.set(event, handlers);
    if (socket) bind(event);
  }
  handlers.add(handler);
  return () => {
    handlers?.delete(handler);
    if (!handlers?.size) listeners.delete(event);
  };
}

export function onPortalConnection(handler: (connected: boolean) => void): () => void {
  lifecycleListeners.add(handler);
  return () => lifecycleListeners.delete(handler);
}

export function emitPortalEvent(event: string, payload?: any, callback?: (response: any) => void) {
  const activeSocket=portalSocket();
  if(!activeSocket)return;
  if(callback)activeSocket.emit(event,payload,callback);
  else activeSocket.emit(event,payload);
}

export function stopPortalRealtime() {
  socket?.disconnect();
  socket = null;
  boundEvents.clear();
}
