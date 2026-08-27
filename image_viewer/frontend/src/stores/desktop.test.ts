import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { saveSort, savedSort, useDesktopStore } from './desktop';
import type { MediaItem } from '@/types';

const image: MediaItem = {
  name: '1000.jpg', path: '12000-14000/1000.jpg', folder: '12000-14000',
  mediaType: 'image', size: 100, mtime: 1, url: '/image_viewer/files/1000.jpg',
};

describe('desktop store', () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });

  it('opens the explorer without replacing existing windows', () => {
    const store = useDesktopStore();
    store.openExplorer('');
    store.openExplorer('12000-14000');
    expect(store.windows).toHaveLength(2);
    expect(store.windows[0].explorer?.folder).toBe('');
    expect(store.windows[1].explorer?.folder).toBe('12000-14000');
  });

  it('stores the file-name sort direction separately for each folder', () => {
    saveSort('12000-14000', 'desc');
    saveSort('動画', 'asc');
    expect(savedSort('12000-14000')).toBe('desc');
    expect(savedSort('動画')).toBe('asc');
    expect(savedSort('0001-2000')).toBe('asc');
  });

  it('opens media as a separate viewer and preserves its sequence', () => {
    const store = useDesktopStore();
    store.openMedia(image, [image]);
    expect(store.windows[0].kind).toBe('image');
    expect(store.windows[0].sequence?.[0].path).toBe(image.path);
  });

  it('restores the next window when the active window closes', () => {
    const store = useDesktopStore();
    const first = store.openExplorer('');
    const second = store.openExplorer('12000-14000');
    store.close(second);
    expect(store.activeId).toBe(first);
  });
});
