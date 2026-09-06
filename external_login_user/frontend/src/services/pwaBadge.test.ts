import { beforeEach, describe, expect, it, vi } from 'vitest';
import { badgeApiUrl, updatePwaBadge } from './pwaBadge';

describe('PWA badge synchronization', () => {
  const setAppBadge = vi.fn().mockResolvedValue(undefined);
  const clearAppBadge = vi.fn().mockResolvedValue(undefined);
  const postMessage = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: setAppBadge });
    Object.defineProperty(navigator, 'clearAppBadge', { configurable: true, value: clearAppBadge });
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: { postMessage },
        getRegistration: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  it('uses the external unread endpoint for participant sessions', async () => {
    await updatePwaBadge(3, 'external');

    expect(setAppBadge).toHaveBeenCalledWith(3);
    expect(postMessage).toHaveBeenCalledWith({
      type: 'SYNC_BADGE',
      badgeApiUrl: '/external-login/api/notifications/unread-count',
      count: 3,
    });
  });

  it('uses the MFU unread endpoint and clears a zero badge', async () => {
    expect(badgeApiUrl('mfu')).toBe('/api/mfu-notifications/unread-count');

    await updatePwaBadge(0, 'mfu');

    expect(clearAppBadge).toHaveBeenCalledOnce();
    expect(postMessage).toHaveBeenCalledWith({
      type: 'SYNC_BADGE',
      badgeApiUrl: '/api/mfu-notifications/unread-count',
      count: 0,
    });
  });
});
