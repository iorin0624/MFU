import type {
  AlbumChild,
  AlbumItem,
  ApiFailure,
  EventItem,
  MediaItem,
  Pagination,
  PortalSession,
} from '@/types';
import { runtimeConfig } from '@/config';

let csrfToken = '';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly payload: ApiFailure = {},
  ) {
    super(message);
  }
}

export function setCsrfToken(value: string): void {
  csrfToken = value || '';
}

async function parseJson<T>(response: Response): Promise<T> {
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) {
    const snippet = (await response.text()).replace(/\s+/g, ' ').slice(0, 120);
    throw new ApiError(`応答を読み取れませんでした（HTTP ${response.status}）${snippet ? `: ${snippet}` : ''}`, response.status);
  }
  const body = await response.json() as T & ApiFailure;
  if (!response.ok || body.ok === false) {
    throw new ApiError(body.message || body.error || `HTTP ${response.status}`, response.status, body);
  }
  return body;
}

export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  headers.set('Accept', 'application/json');
  if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    cache: 'no-store',
    headers,
  });
  return parseJson<T>(response);
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

export const portalApi = {
  bootstrap: () => requestJson<{ok: true; session: PortalSession; events: EventItem[]}>(runtimeConfig.bootstrapUrl),
  events: (scope = 'all', page = 1) => requestJson<{ok: true; events: EventItem[]; pagination: Pagination}>(
    `${runtimeConfig.eventsUrl}?scope=${encoded(scope)}&page=${page}&perPage=50`,
  ),
  event: (uuid: string) => requestJson<{ok: true; event: EventItem}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}`),
  logout: () => requestJson<{ok: true; loggedOut: boolean}>(`${runtimeConfig.bootstrapUrl.replace(/\/bootstrap$/, '/logout')}`, { method: 'POST' }),
  album: (albumId: string) => requestJson<{ok: true; album: AlbumItem}>(`${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}`),
  children: (albumId: string) => requestJson<{ok: true; children: AlbumChild[]; permissions: AlbumItem['permissions']}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children`,
  ),
  createChild: (albumId: string, name: string, mode: AlbumChild['mode']) => requestJson<{ok: true; child: AlbumChild}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children`,
    { method: 'POST', body: JSON.stringify({ name, mode }) },
  ),
  renameChild: (albumId: string, childId: string, name: string) => requestJson<{ok: true; child: AlbumChild}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}`,
    { method: 'PATCH', body: JSON.stringify({ name }) },
  ),
  deleteChild: (albumId: string, childId: string) => requestJson<{ok: true; deleted: boolean}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}`,
    { method: 'DELETE' },
  ),
  media: (albumId: string, childId: string, page = 1) => requestJson<{
    ok: true; child: AlbumChild; media: MediaItem[]; pagination: Pagination; permissions: AlbumChild['permissions'];
  }>(`${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media?page=${page}&perPage=100`),
  uploadMedia: (albumId: string, childId: string, files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append('file', file, file.name));
    return requestJson<{ok: true; uploaded: boolean; mediaCount: number}>(
      `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media`,
      { method: 'POST', body: form },
    );
  },
  deleteMedia: (albumId: string, childId: string, names: string[]) => requestJson<{ok: true; deleted: string[]; missing: string[]}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media`,
    { method: 'DELETE', body: JSON.stringify({ names }) },
  ),
};
