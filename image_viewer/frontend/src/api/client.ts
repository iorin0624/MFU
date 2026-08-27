import { runtimeConfig } from '@/config';
import type { ApiErrorPayload, ImageListPayload } from '@/types';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly payload: ApiErrorPayload = {},
  ) {
    super(message);
  }
}

function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
}

async function parseResponse<T>(response: Response): Promise<T> {
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) {
    const text = (await response.text()).replace(/\s+/g, ' ').slice(0, 100);
    const message = response.status === 413
      ? 'アップロード容量が大きすぎます。ファイルを分けてください。'
      : `応答を読み取れませんでした（HTTP ${response.status}）${text ? `: ${text}` : ''}`;
    throw new ApiError(message, response.status);
  }
  const payload = await response.json() as T & ApiErrorPayload;
  if (!response.ok || payload.ok === false) {
    throw new ApiError(
      payload.message || payload.error || `HTTP ${response.status}`,
      response.status,
      payload,
    );
  }
  return payload;
}

async function rawRequest<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  headers.set('Accept', 'application/json');
  const token = csrfToken();
  if (token) headers.set('X-CSRF-Token', token);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  return parseResponse<T>(await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers,
  }));
}

export async function requestJson<T>(
  url: string,
  init: RequestInit = {},
  passkeyAction = '',
): Promise<T> {
  try {
    return await rawRequest<T>(url, init);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 428 || !passkeyAction) throw error;
    if (!window.MFUAdminPasskey) {
      throw new Error('この操作には管理者パスキー認証が必要です。');
    }
    const action = error.payload.action || passkeyAction;
    const grant = await window.MFUAdminPasskey.authorize(action);
    const headers = new Headers(init.headers || {});
    headers.set('X-MFU-Admin-Passkey', grant);
    return rawRequest<T>(url, { ...init, headers });
  }
}

function queryUrl(base: string, values: Record<string, string | number | undefined>): string {
  const url = new URL(base, window.location.origin);
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
  });
  return `${url.pathname}${url.search}`;
}

export const imageViewerApi = {
  list(folder: string, sort: 'asc' | 'desc', page = 1, perPage = 1000, center?: number) {
    return requestJson<ImageListPayload>(queryUrl(runtimeConfig.imagesUrl, {
      folder, sort, page, perPage, center,
    }));
  },
  version(folder: string) {
    return requestJson<{ok: boolean; version: string}>(queryUrl(runtimeConfig.imagesVersionUrl, {
      folder, _: Date.now(),
    }));
  },
  createFolder(parent: string, name: string) {
    return requestJson<{ok: true; folder: string}>(runtimeConfig.createFolderUrl, {
      method: 'POST', body: JSON.stringify({ parent, name }),
    });
  },
  properties(path: string) {
    return requestJson<Record<string, unknown>>(queryUrl(runtimeConfig.propertiesUrl, { path }));
  },
  rename(path: string, name: string, type = 'file') {
    return requestJson<{ok: true; path: string; folder?: string}>(runtimeConfig.renameUrl, {
      method: 'POST', body: JSON.stringify({ path, name, type }),
    });
  },
  renameFolder(path: string, name: string) {
    return requestJson<{ok: true; path: string; folder?: string}>(runtimeConfig.renameUrl, {
      method: 'POST', body: JSON.stringify({ path, name, type: 'folder' }),
    });
  },
  renameStem(path: string, stem: string) {
    return requestJson<Record<string, unknown>>(runtimeConfig.propertiesUrl, {
      method: 'POST', body: JSON.stringify({ path, stem }),
    });
  },
  delete(paths: string[]) {
    return requestJson<Record<string, unknown>>(runtimeConfig.deleteUrl, {
      method: 'POST',
      body: JSON.stringify({ entries: paths.map((path) => ({ path, type: 'file' })) }),
    }, 'image_delete');
  },
  deleteFolder(path: string) {
    return requestJson<Record<string, unknown>>(runtimeConfig.deleteUrl, {
      method: 'POST', body: JSON.stringify({ path, type: 'folder' }),
    }, 'image_folder_delete');
  },
  move(paths: string[], destination: string) {
    return requestJson<Record<string, unknown>>(runtimeConfig.moveUrl, {
      method: 'POST',
      body: JSON.stringify({ destination, entries: paths.map((path) => ({ path, type: 'file' })) }),
    });
  },
  moveFolder(path: string, destination: string) {
    return requestJson<{ok: true; path: string; folder?: string}>(runtimeConfig.moveUrl, {
      method: 'POST', body: JSON.stringify({ path, destination, type: 'folder' }),
    });
  },
  copy(paths: string[], destination: string) {
    return requestJson<Record<string, unknown>>(runtimeConfig.copyUrl, {
      method: 'POST',
      body: JSON.stringify({ destination, entries: paths.map((path) => ({ path, type: 'file' })) }),
    });
  },
  appendSequence(sources: string[], target: string) {
    return requestJson<Record<string, unknown>>(runtimeConfig.appendSequenceUrl, {
      method: 'POST', body: JSON.stringify({ sources, target }),
    });
  },
  upload(files: File[], folder: string, numbering: boolean, paste = false) {
    const form = new FormData();
    form.set('folder', folder);
    form.set('numbering', numbering ? '1' : '0');
    files.forEach((file) => form.append('files', file, file.name));
    return requestJson<{ok: boolean; saved: unknown[]; skipped: string[]; errors: unknown[]}>(
      paste ? runtimeConfig.pasteUrl : runtimeConfig.uploadUrl,
      { method: 'POST', body: form },
    );
  },
  startThumbnails(folder: string, force: boolean) {
    return requestJson<{ok: true; jobId: string}>(runtimeConfig.thumbnailUrl, {
      method: 'POST', body: JSON.stringify({ folder, force }),
    });
  },
  thumbnailJob(jobId: string) {
    return requestJson<Record<string, unknown>>(
      runtimeConfig.thumbnailJobUrl.replace('__JOB_ID__', encodeURIComponent(jobId)),
    );
  },
};
