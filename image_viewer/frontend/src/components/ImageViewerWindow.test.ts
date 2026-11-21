import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, shallowMount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { runtimeConfig } from '@/config';
import { imageViewerApi } from '@/api/client';
import { useDesktopStore } from '@/stores/desktop';
import { useNotificationStore } from '@/stores/notifications';
import { writeImageToClipboard } from '@/utils/imageClipboard';
import type { MediaItem } from '@/types';
import ImageViewerWindow from './ImageViewerWindow.vue';

vi.mock('@/config', () => ({ runtimeConfig: { isAdmin: true } }));
vi.mock('@/api/client', () => ({ imageViewerApi: { cropStatus: vi.fn(), transferSmb: vi.fn() } }));
vi.mock('@/utils/imageClipboard', () => ({ writeImageToClipboard: vi.fn().mockResolvedValue(undefined) }));

const item: MediaItem = {
  name: 'original.jpg', path: 'regular/original.jpg', folder: 'regular', mediaType: 'image', size: 1, mtime: 1, url: '/original.jpg',
};
const reply: MediaItem = { ...item, path: '../7/24', url: '/reply.jpg', readOnly: true };

beforeEach(() => {
  vi.clearAllMocks();
  runtimeConfig.isAdmin = true;
  vi.mocked(imageViewerApi.cropStatus).mockResolvedValue({ ok: true, hasOriginal: false });
});

function open(...images: MediaItem[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const desktop = useDesktopStore();
  desktop.openMedia(images[0], images);
  return shallowMount(ImageViewerWindow, {
    props: { win: desktop.windows[0] }, global: { plugins: [pinia], stubs: { teleport: true } },
  });
}

it('copies the displayed image and offers DL and transfer together', async () => {
  const wrapper = open(item, reply);
  await wrapper.get('button[title="次の画像"]').trigger('click');
  const toolbar = wrapper.findAll('.viewer-toolbar button');
  const rotate = toolbar.findIndex((button) => button.text() === '回転');
  expect(toolbar[rotate + 1].text()).toBe('コピー（📎）');
  await toolbar[rotate + 1].trigger('click');
  expect(writeImageToClipboard).toHaveBeenCalledWith(reply.url);

  vi.mocked(imageViewerApi.transferSmb).mockResolvedValue({ ok: true, succeeded: 1, failed: 0, results: [{ ok: true, path: reply.path, name: 'saved.jpg' }] });
  await wrapper.get('[aria-haspopup="menu"]').trigger('click');
  const entries = wrapper.findAll('[role="menuitem"]');
  expect(entries.map((button) => button.text())).toEqual(['画像DL', 'ラズパイに転送']);
  await entries[1].trigger('click');
  await flushPromises();
  expect(imageViewerApi.transferSmb).toHaveBeenCalledWith([reply.path]);
  expect(useNotificationStore().message).toContain('saved.jpg');
  useNotificationStore().clear();
  wrapper.unmount();
});

it('shows only download to a non-admin and closes the menu with Escape', async () => {
  runtimeConfig.isAdmin = false;
  const wrapper = open(reply);
  const trigger = wrapper.get('[aria-haspopup="menu"]');
  await trigger.trigger('click');
  expect(wrapper.findAll('[role="menuitem"]').map((button) => button.text())).toEqual(['画像DL']);
  await wrapper.get('[role="menu"]').trigger('keydown', { key: 'Escape' });
  expect(trigger.attributes('aria-expanded')).toBe('false');
  expect(imageViewerApi.transferSmb).not.toHaveBeenCalled();
  wrapper.unmount();
});
