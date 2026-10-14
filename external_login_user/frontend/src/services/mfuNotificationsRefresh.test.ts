import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

const templatePath = resolve(process.cwd(), '../template/notifications.html');
const template = readFileSync(templatePath, 'utf8');
const script = [...template.matchAll(/<script>\s*([\s\S]*?)\s*<\/script>/g)].at(-1)?.[1]
  ?.replace('{{ notification_scope or "external" }}', 'mfu');

function notice(id: number) {
  return { id, title: `通知${id}`, feature_key: 'general', content: {}, target_url: '/mfu-notifications/' };
}

function mountNotifications() {
  if (!script) throw new Error('notification script not found');
  document.body.innerHTML = `
    <div id="notificationList"></div>
    <div id="emptyState" class="d-none"></div>
    <button id="loadMoreBtn"></button><button id="markAllReadBtn"></button>
    <button id="refreshBtn"></button><input id="onlyUnread" type="checkbox" checked>
  `;
  localStorage.clear();
  let hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });

  const socketHandlers: Record<string, (payload?: unknown) => void> = {};
  const socket = { on: (name: string, handler: (payload?: unknown) => void) => { socketHandlers[name] = handler; } };
  Object.assign(window, {
    MFU_NOTIFICATION_CONFIG: {
      scope: 'mfu',
      urls: {
        list: '/api/mfu-notifications',
        updates: '/api/mfu-notifications/updates',
        unreadCount: '/api/mfu-notifications/unread-count',
        readOneBase: '/api/mfu-notifications',
        readAll: '/api/mfu-notifications/read-all',
      },
      channels: [],
    },
    io: () => socket,
  });

  const items = [notice(1)];
  const requests: URL[] = [];
  const fetchMock = vi.fn(async (input: string) => {
    const url = new URL(input, 'https://mfu.iori0624.jp');
    requests.push(url);
    const since = Number(url.searchParams.get('since_id') || 0);
    const data = url.pathname.endsWith('/updates')
      ? { items: items.filter((item) => item.id > since), latest_id: Math.max(since, ...items.map((item) => item.id)), unread_count: items.length, channel_unread: {} }
      : url.pathname.endsWith('/unread-count')
        ? { unread_count: items.length, channel_unread: {} }
        : { items: [...items].reverse(), unread_count: items.length, channel_unread: {}, pagination: { has_next: false } };
    return { ok: true, json: async () => data };
  });
  let tick: (() => Promise<void>) | undefined;
  vi.spyOn(window, 'setInterval').mockImplementation(((handler: () => Promise<void>, delay: number) => {
    expect(delay).toBe(15000);
    tick = handler;
    return 1;
  }) as typeof window.setInterval);
  vi.spyOn(window, 'clearInterval').mockImplementation(() => undefined);

  new Function('window', 'document', 'navigator', 'fetch', script)(window, document, navigator, fetchMock);
  const ids = () => [...document.querySelectorAll('#notificationList > article')]
    .map((element) => Number((element as HTMLElement).dataset.notificationId));
  return { items, requests, socketHandlers, ids, getTick: () => tick, setHidden: (value: boolean) => { hidden = value; } };
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
  delete (window as Window & { io?: unknown }).io;
});

describe('MFU notification auto-refresh', () => {
  it('polls even with the default unread filter and a connected socket, without duplicates', async () => {
    const state = mountNotifications();
    await vi.waitFor(() => expect(state.ids()).toEqual([1]));
    state.socketHandlers.connect();
    await vi.waitFor(() => expect(state.getTick()).toBeTypeOf('function'));
    await vi.waitFor(() => expect(state.requests.some((url) => url.pathname.endsWith('/updates'))).toBe(true));
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    state.items.push(notice(2));
    await state.getTick()?.();
    expect(state.ids()).toEqual([2, 1]);
    expect(state.requests.some((url) => url.pathname.endsWith('/updates') && url.searchParams.get('unread') === '1')).toBe(true);

    state.socketHandlers.notif_new({ item: notice(3) });
    state.items.push(notice(3));
    await state.getTick()?.();
    expect(state.ids()).toEqual([3, 2, 1]);
  });

  it('refreshes immediately when the notification window returns to the foreground', async () => {
    const state = mountNotifications();
    await vi.waitFor(() => expect(state.ids()).toEqual([1]));
    state.setHidden(true);
    state.items.push(notice(2));
    await state.getTick()?.();
    expect(state.ids()).toEqual([1]);

    state.setHidden(false);
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 2000);
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(state.ids()).toEqual([2, 1]));
  });
});
