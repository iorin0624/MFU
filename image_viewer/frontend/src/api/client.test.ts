import { beforeEach, describe, expect, it, vi } from 'vitest';

const config = {
  imagesUrl: '/image_viewer/api/images',
  imagesVersionUrl: '/image_viewer/api/images/version', createFolderUrl: '/image_viewer/api/folders',
  propertiesUrl: '/image_viewer/api/entries/properties', renameUrl: '/image_viewer/api/entries/rename',
  appendSequenceUrl: '/image_viewer/api/entries/append-sequence', deleteUrl: '/image_viewer/api/entries/delete',
  moveUrl: '/image_viewer/api/entries/move', copyUrl: '/image_viewer/api/entries/copy',
  uploadUrl: '/image_viewer/api/upload', pasteUrl: '/image_viewer/api/paste',
  thumbnailUrl: '/image_viewer/api/thumbnails', thumbnailJobUrl: '/image_viewer/api/thumbnails/jobs/__JOB_ID__',
  isAdmin: true,
};

describe('image viewer API client', () => {
  beforeEach(() => {
    vi.resetModules();
    document.head.innerHTML = '<meta name="csrf-token" content="csrf-test">';
    document.body.innerHTML = `<script id="image-viewer-config" type="application/json">${JSON.stringify(config)}</script>`;
    vi.restoreAllMocks();
  });

  it('sends the CSRF token and same-origin credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, folders: [], images: [] }), {
      status: 200, headers: { 'content-type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const { imageViewerApi } = await import('./client');
    await imageViewerApi.list('', 'asc');
    const [, request] = fetchMock.mock.calls[0];
    expect(request.credentials).toBe('same-origin');
    expect((request.headers as Headers).get('X-CSRF-Token')).toBe('csrf-test');
  });

  it('reports a readable error for an HTML 500 response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<!doctype html><title>500</title>', {
      status: 500, headers: { 'content-type': 'text/html' },
    })));
    const { imageViewerApi } = await import('./client');
    await expect(imageViewerApi.list('', 'asc')).rejects.toThrow('HTTP 500');
  });
});
