import type {
  AlbumChild,
  AlbumItem,
  AlbumDownloadJob,
  ApiFailure,
  EventItem,
  EventMemberItem,
  MediaItem,
  Pagination,
  ParticipantPass,
  ProcessingState,
  PortalSession,
  ShortcutDownloadJob,
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

function passkeyHeader(token?: string): HeadersInit | undefined {
  return token ? { 'X-MFU-Admin-Passkey': token } : undefined;
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
  participantPass: (uuid: string) => requestJson<{ok: true; participantPass: ParticipantPass}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/pass`,
  ),
  eventMembers: (uuid: string) => requestJson<{ok: true; members: EventMemberItem[]}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/members`,
  ),
  requestParticipantsEmail: (uuid: string) => requestJson<{ok: true; accepted: boolean; message: string}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/participants-email`, { method: 'POST' },
  ),
  notifications: (scope: 'external' | 'mfu', page = 1, unread = false) => requestJson<{items: Array<{id: number; kind?: string; title?: string; body: string; target_url: string; room_name?: string; created_at?: string; read_at?: string}>; pagination: {page?: number; per_page?: number; total?: number; has_next: boolean}}>(
    `${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}?page=${page}&unread=${unread ? '1' : '0'}`,
  ),
  markNotificationRead: (scope: 'external' | 'mfu', id: number) => requestJson<{ok: true}>(`${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}/${id}/read`, { method: 'POST' }),
  markAllNotificationsRead: (scope: 'external' | 'mfu') => requestJson<{ok: true; updated: number}>(`${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}/read-all`, { method: 'POST' }),
  saveMyEventRole: (uuid: string, participantRole: string, costumeLabel: string) => requestJson<{ok: true; participantRole: string; costumeLabel?: string | null}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/my-role`,
    { method: 'POST', body: JSON.stringify({ participantRole, costumeLabel }) },
  ),
  saveMyEventProcess: (uuid: string, process: boolean) => requestJson<{ok: true; process: boolean}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/my-process`,
    { method: 'POST', body: JSON.stringify({ process }) },
  ),
  profile: () => requestJson<{ok: true; profile: Record<string, unknown>}>('/external-login/api/vue/profile'),
  saveProfile: (form: FormData) => requestJson<{ok: true; saved: boolean; emailVerificationRequired: boolean; verificationSent: boolean}>(
    '/external-login/api/vue/profile', { method: 'POST', body: form },
  ),
  sendEmailVerification: () => requestJson<{ok: true; sent: boolean; email: string}>('/external-login/api/vue/email-verification/send', { method: 'POST' }),
  verifyEmail: (pin: string) => requestJson<{ok: true; verified: boolean; nextUrl?: string}>('/external-login/api/vue/email-verification/verify', { method: 'POST', body: JSON.stringify({ pin }) }),
  requestLoginPin: (email: string) => requestJson<{ok: boolean; message: string}>('/external-login/pin/request', { method: 'POST', body: JSON.stringify({ email }) }),
  loginWithPin: (email: string, pin: string) => requestJson<{ok: true; loggedIn: boolean; nickname: string}>('/external-login/pin/login', { method: 'POST', body: JSON.stringify({ email, pin }) }),
  joinInfo: (uuid: string, iv = '') => requestJson<{ok: true; join: Record<string, any>}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}/join${iv ? `?iv=${encoded(iv)}` : ''}`),
  updatesCheck: () => requestJson<{show: boolean; text?: string; hash?: string; seen?: boolean}>('/external-login/updates/check'),
  updatesAck: () => requestJson<{ok: true}>('/external-login/updates/ack', { method: 'POST' }),
  logout: () => requestJson<{ok: true; loggedOut: boolean}>(`${runtimeConfig.bootstrapUrl.replace(/\/bootstrap$/, '/logout')}`, { method: 'POST' }),
  album: (albumId: string) => requestJson<{ok: true; album: AlbumItem}>(`${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}`),
  renameAlbum: (albumId: string, name: string) => requestJson<{ok: true; album: Pick<AlbumItem, 'id' | 'name'>}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}`,
    { method: 'PATCH', body: JSON.stringify({ name }) },
  ),
  deleteAlbum: (albumId: string, passkeyToken?: string) => requestJson<{ok: true; deleted: boolean}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}`,
    { method: 'DELETE', headers: passkeyHeader(passkeyToken) },
  ),
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
  deleteChild: (albumId: string, childId: string, passkeyToken?: string) => requestJson<{ok: true; deleted: boolean}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}`,
    { method: 'DELETE', headers: passkeyHeader(passkeyToken) },
  ),
  media: (albumId: string, childId: string, page = 1, sort = 'asc', search = '') => requestJson<{
    ok: true; child: AlbumChild; media: MediaItem[]; pagination: Pagination; permissions: AlbumChild['permissions'];
  }>(`${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media?page=${page}&perPage=100&sort=${encoded(sort)}&search=${encoded(search)}`),
  uploadMedia: (albumId: string, childId: string, files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append('file', file, file.name));
    return requestJson<{ok: true; uploaded: boolean; mediaCount: number}>(
      `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media`,
      { method: 'POST', body: form },
    );
  },
  renameMedia: (albumId: string, childId: string, filename: string, name: string) => requestJson<{ok: true; renamed: boolean; name: string}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media/${encoded(filename)}`,
    { method: 'PATCH', body: JSON.stringify({ name }) },
  ),
  deleteMedia: (albumId: string, childId: string, names: string[], passkeyToken?: string) => requestJson<{ok: true; deleted: string[]; missing: string[]}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media`,
    { method: 'DELETE', headers: passkeyHeader(passkeyToken), body: JSON.stringify({ names }) },
  ),
  processing: (albumId: string, childId: string) => requestJson<{ok: true; processing: ProcessingState}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/processing`,
  ),
  beginProcessing: (albumId: string, childId: string) => requestJson<{ok: true; downloadUrl: string; processing: ProcessingState}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/processing/begin`,
    { method: 'POST' },
  ),
  unlockProcessing: (albumId: string, childId: string, force = false) => requestJson<{ok: true; processing: ProcessingState}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/processing/${force ? 'force-unlock' : 'unlock'}`,
    { method: 'POST' },
  ),
  saveProcessingRequests: (albumId: string, childId: string, members: Array<{ext_user_id: number; request_flag: boolean; complete_flag: boolean}>) => requestJson<{ok: true; sent: number}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/processing/requests`,
    { method: 'PUT', body: JSON.stringify({ members }) },
  ),
  saveProcessingMember: (albumId: string, childId: string, extUserId: number, requestFlag: boolean, completeFlag: boolean) => requestJson<{ok: true}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/processing/members/${extUserId}`,
    { method: 'PUT', body: JSON.stringify({ request_flag: requestFlag, complete_flag: completeFlag }) },
  ),
  createAlbumDownload: (albumId: string, childId: string, names: string[]) => requestJson<{ok: true; job: AlbumDownloadJob}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/download-jobs`,
    { method: 'POST', body: JSON.stringify({ childId, names }) },
  ),
  albumDownloadStatus: (albumId: string, jobId: string) => requestJson<{ok: true; job: AlbumDownloadJob}>(
    `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/download-jobs/${encoded(jobId)}`,
  ),
  createShortcutDownload: (albumId: string, childId: string, filenames: string[]) => requestJson<ShortcutDownloadJob>(
    '/mobile-download/api/jobs',
    { method: 'POST', body: JSON.stringify({ source_type: 'album', album_id: albumId, child_id: childId, filenames }) },
  ),
};
