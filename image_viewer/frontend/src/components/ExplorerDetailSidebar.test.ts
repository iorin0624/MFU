import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import ExplorerDetailSidebar from './ExplorerDetailSidebar.vue';
import { imageViewerApi } from '@/api/client';
import type { MediaItem } from '@/types';

vi.mock('@/api/client', () => ({
  imageViewerApi: {
    properties: vi.fn(), styleTags: vi.fn(), saveStyleTags: vi.fn(), clearStyleTags: vi.fn(),
  },
}));

const image: MediaItem = {
  name: 'photo.jpg', path: 'album/photo.jpg', folder: 'album',
  mediaType: 'image', size: 2048, mtime: 1, url: '/image_viewer/files/photo.jpg',
};

describe('ExplorerDetailSidebar', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.mocked(imageViewerApi.properties).mockReset().mockResolvedValue({
      size: 2048, width: 1200, height: 800, sourceUrl: 'https://example.com/photo',
    });
    vi.mocked(imageViewerApi.styleTags).mockReset().mockResolvedValue({
      ok: true, majorStyles: [], assignment: null, gpuJob: null,
    });
    vi.mocked(imageViewerApi.saveStyleTags).mockReset().mockResolvedValue({
      ok: true, majorStyles: [], assignment: null, gpuJob: null,
    });
    vi.mocked(imageViewerApi.clearStyleTags).mockReset().mockResolvedValue({
      ok: true, majorStyles: [], assignment: null, gpuJob: null,
    });
  });

  it('shows selected image properties and saves a major tag without a save button', async () => {
    const wrapper = mount(ExplorerDetailSidebar, {props: {item: image}});
    await flushPromises();
    expect(wrapper.text()).toContain('photo.jpg');
    expect(wrapper.text()).toContain('2.00 KB');
    expect(wrapper.text()).toContain('1,200 × 800 px');
    expect(wrapper.find('a[href="https://example.com/photo"]').exists()).toBe(true);
    await wrapper.get('select').setValue('lolita');
    await flushPromises();
    expect(imageViewerApi.saveStyleTags).toHaveBeenCalledWith(image.path, {
      major: 'lolita', middleTags: [],
    });
    wrapper.unmount();
  });

  it('flushes pending tag edits when selection changes', async () => {
    const wrapper = mount(ExplorerDetailSidebar, {props: {item: image}});
    await flushPromises();
    await wrapper.get('select').setValue('jirai');
    await flushPromises();
    await wrapper.get('.details-tags > button').trigger('click');
    await wrapper.get('input[placeholder="例: クラシカル"]').setValue('ゴシック');
    await wrapper.setProps({item: {...image, name: 'next.jpg', path: 'album/next.jpg'}});
    await flushPromises();
    expect(imageViewerApi.saveStyleTags).toHaveBeenCalledWith(image.path, {
      major: 'jirai', middleTags: [{name: 'ゴシック', smallTags: []}],
    });
    expect(imageViewerApi.properties).toHaveBeenCalledWith('album/next.jpg');
    wrapper.unmount();
  });
});
