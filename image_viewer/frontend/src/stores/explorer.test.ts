import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

vi.mock('@/api/client', () => ({
  imageViewerApi: {
    list: vi.fn().mockResolvedValue({
      ok: true,
      folders: ['', '動画'],
      images: [{ name: '1.mp4', path: '動画/1.mp4', folder: '動画', mediaType: 'video', size: 1, mtime: 1, url: '/1.mp4' }],
      pagination: { page: 1, perPage: 401, total: 1, pages: 1, hasMore: false, offset: 0 },
    }),
  },
}));

describe('explorer store', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('keeps media returned with paginated folder metadata', async () => {
    const { useExplorerStore } = await import('./explorer');
    const store = useExplorerStore();
    await store.load('動画', 'asc', true);
    expect(store.dataFor('動画', 'asc').total).toBe(1);
    expect(store.dataFor('動画', 'asc').items).toHaveLength(1);
  });
});

