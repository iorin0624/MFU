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
  ChatBootstrap,
  ChatMessage,
  ChatRoom,
  ChatRoomMember,
  ChatReadState,
  ChatVueSession,
} from '@/types';
import { runtimeConfig } from '@/config';

let csrfToken = '';
let chatCsrfToken = '';
let chatAuthScope = '';

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

export function setChatCsrfToken(value: string): void {
  chatCsrfToken = value || '';
}

export function setChatAuthScope(value: string): void {
  chatAuthScope = value === 'mfu' ? 'mfu' : '';
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
  if (chatAuthScope && url.startsWith('/chat/')) headers.set('X-Chat-Auth-Scope', chatAuthScope);
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

/** XHR is used only for upload byte progress; authentication matches requestJson. */
export function uploadMediaWithProgress(albumId: string, childId: string, files: File[], progress: (fraction: number) => void, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    files.forEach(file => form.append('file', file, file.name));
    xhr.open('POST', `${runtimeConfig.albumApiBase}/albums/${encoded(albumId)}/children/${encoded(childId)}/media`);
    xhr.withCredentials = true;
    xhr.timeout = 10 * 60 * 1000;
    xhr.setRequestHeader('Accept', 'application/json');
    if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    const abort = () => xhr.abort();
    const cleanup = () => signal?.removeEventListener('abort', abort);
    xhr.upload.onprogress = event => { if (event.lengthComputable && event.total) progress(event.loaded / event.total); };
    xhr.onerror = () => { cleanup(); reject(new Error('通信に失敗しました。サーバー側で保存済みの場合もあるため、一覧を確認してください。')); };
    xhr.ontimeout = () => { cleanup(); reject(new Error('送信がタイムアウトしました。一覧を確認してください。')); };
    xhr.onabort = () => { cleanup(); reject(new Error('送信を中止しました。')); };
    xhr.onload = async () => {
      cleanup();
      try {
        await parseJson(new Response(xhr.responseText, {status: xhr.status, headers: {'Content-Type': xhr.getResponseHeader('Content-Type') || ''}}));
        resolve();
      } catch (error) { reject(error); }
    };
    if (signal?.aborted) { reject(new Error('送信を中止しました。')); return; }
    signal?.addEventListener('abort', abort, {once:true});
    xhr.send(form);
  });
}

export const portalApi = {
  chatSession: () => requestJson<ChatVueSession>('/chat/api/vue/session'),
  eventChatUnreadCounts: (ids: number[]) => requestJson<{ok:true;counts:Record<string,number>}>(
    `/external-login/api/events/chat-unread-counts?event_ids=${encoded(ids.join(','))}`,
  ),
  bootstrap: () => requestJson<{ok: true; session: PortalSession; events: EventItem[]}>(runtimeConfig.bootstrapUrl),
  events: (scope = 'all', page = 1) => requestJson<{ok: true; events: EventItem[]; pagination: Pagination}>(
    `${runtimeConfig.eventsUrl}?scope=${encoded(scope)}&page=${page}&perPage=50`,
  ),
  event: (uuid: string) => requestJson<{ok: true; event: EventItem}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}`),
  agreePrivacyPolicy: () => requestJson<{ok:true;agreed:boolean}>('/external-login/api/vue/privacy-policy/agree', {method:'POST'}),
  paymentOptions: (uuid:string) => requestJson<{ok:true;payment:Record<string,any>}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}/payment-options`),
  submitPayPay: (uuid:string, form:FormData) => requestJson<{ok:true;submitted:boolean}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}/payment-paypay`, {method:'POST',body:form}),
  submitBankPayment: (uuid:string, form:FormData) => requestJson<{ok:true;submitted:boolean}>(`${runtimeConfig.eventsUrl}/${encoded(uuid)}/payment-bank`, {method:'POST',body:form}),
  participantPass: (uuid: string) => requestJson<{ok: true; participantPass: ParticipantPass}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/pass`,
  ),
  eventMembers: (uuid: string) => requestJson<{ok: true; members: EventMemberItem[]}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/members`,
  ),
  requestParticipantsEmail: (uuid: string) => requestJson<{ok: true; accepted: boolean; message: string}>(
    `${runtimeConfig.eventsUrl}/${encoded(uuid)}/participants-email`, { method: 'POST' },
  ),
  notifications: (scope: 'external' | 'mfu', page = 1, unread = false, category: 'all' | 'notice' | 'chat' = 'all') => requestJson<{items: Array<{id: number; kind?: string; title?: string; body: string; target_url: string; room_name?: string; created_at?: string; read_at?: string}>; unread?: {total: number; notifications: number; chat: number}; pagination: {page?: number; per_page?: number; total?: number; has_next: boolean}}>(
    `${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}?page=${page}&unread=${unread ? '1' : '0'}&category=${category}`,
  ),
  notificationUnread: (scope: 'external' | 'mfu') => requestJson<{count: number; unread_count?: number; total: number; notifications: number; chat: number}>(
    scope === 'mfu' ? '/api/mfu-notifications/unread-count' : '/external-login/api/notifications/unread-count',
  ),
  markNotificationRead: (scope: 'external' | 'mfu', id: number) => requestJson<{ok: true}>(`${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}/${id}/read`, { method: 'POST' }),
  markAllNotificationsRead: (scope: 'external' | 'mfu') => requestJson<{ok: true; updated: number}>(`${scope === 'mfu' ? '/api/mfu-notifications' : '/external-login/api/notifications'}/read-all`, { method: 'POST' }),
  markChatRoomNotificationsRead: (scope: 'external' | 'mfu', eventId: number, roomId: string) => requestJson<{ok:true;updated_count:number;unread_count:number}>(
    scope === 'mfu' ? '/api/mfu-notifications/read-by-room' : '/external-login/api/notifications/read-by-room',
    { method:'POST', body:JSON.stringify({ event_id:eventId, room_id:roomId }) },
  ),
  markDmRoomNotificationsRead: (scope: 'external' | 'mfu', dmUuid: string) => requestJson<{ok:true;updated_count:number;unread_count:number}>(
    scope === 'mfu' ? '/api/mfu-notifications/read-by-room' : '/external-login/api/notifications/read-dm-room',
    { method:'POST', body:JSON.stringify({ room_id:`dm:${dmUuid}` }) },
  ),
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
  chatBootstrap: () => requestJson<{ok: true} & ChatBootstrap>('/chat/api/vue/bootstrap'),
  chatEventSnapshot: (eventId: number, roomId = '') => requestJson<{
    ok: true; event: {id: number; title: string; start_at?: string}; active_room: ChatRoom;
    accessible_rooms: ChatRoom[]; can_manage_rooms: boolean; messages: ChatMessage[];
    read_states: ChatReadState[]; csrf_token: string;
  }>(`/chat/api/vue/events/${eventId}/snapshot${roomId ? `?room_id=${encoded(roomId)}` : ''}`),
  chatOlderMessages: (eventId: number, roomId: string, beforeId: number, limit = 50) => requestJson<{
    ok: true; messages: ChatMessage[]; has_more: boolean;
  }>(`/chat/api/vue/events/${eventId}/messages?room_id=${encoded(roomId)}&before_id=${beforeId}&limit=${limit}`),
  chatSearch: (eventId: number, roomId: string, query: string) => requestJson<{ok: true; results: Array<Record<string, any>>}>(
    `/chat/api/vue/events/${eventId}/search?room_id=${encoded(roomId)}&q=${encoded(query)}`,
  ),
  chatThreads: (eventId: number, roomId: string, messageId: number) => requestJson<{ok: true; root: ChatMessage; replies: ChatMessage[]}>(
    `/chat/api/events/${eventId}/threads/${messageId}?room_id=${encoded(roomId)}`,
  ),
  chatReactionDetails: (eventId: number, roomId: string, messageId: number) => requestJson<{ok: true; groups: Array<{emoji:string;count:number;actors:Array<{actor_key:string;display_name:string}>}>}>(
    `/chat/api/events/${eventId}/messages/${messageId}/reactions?room_id=${encoded(roomId)}`,
  ),
  chatUploadImages: (eventId: number, roomId: string, files: File[], body = '') => {
    const form = new FormData();
    form.append('room_id', roomId);
    form.append('body', body);
    if (chatCsrfToken) form.append('csrf_token', chatCsrfToken);
    files.forEach((file) => form.append('file', file, file.name));
    return requestJson<{ok: true; message?: ChatMessage; messages?: ChatMessage[]}>(
      `/chat/api/events/${eventId}/upload-image`, { method: 'POST', body: form },
    );
  },
  chatEditMessage: (eventId: number, roomId: string, messageId: number, body: string) => requestJson<{ok: true; message: ChatMessage}>(
    `/chat/api/events/${eventId}/messages/${messageId}/edit`, { method: 'POST', body: JSON.stringify({ room_id: roomId, body, csrf_token: chatCsrfToken }) },
  ),
  chatDeleteMessage: (eventId: number, roomId: string, messageId: number, deleteMode: 'cancel'|'admin' = 'cancel') => requestJson<{ok: true; message: ChatMessage}>(
    `/chat/api/events/${eventId}/messages/${messageId}/delete`, { method: 'POST', body: JSON.stringify({ room_id: roomId, delete_mode: deleteMode, csrf_token: chatCsrfToken }) },
  ),
  chatMuteRoom: (eventId: number, roomId: string, hours?: number) => requestJson<{ok: true}>(
    `/chat/api/events/${eventId}/rooms/${encoded(roomId)}/mute`, { method: 'POST', body: JSON.stringify({ muted_until: hours ? new Date(Date.now() + hours * 3600000).toISOString() : null, csrf_token: chatCsrfToken }) },
  ),
  chatRoomUnread: (eventId: number) => requestJson<{ok: true; rooms: Array<{room_id:string;unread_count:number;first_unread_id?:number|null}>}>(
    `/chat/api/events/${eventId}/rooms/unread`,
  ),
  chatCreateRoom: (eventId: number, roomName: string, memberActorKeys: string[]) => requestJson<{ok: true; room_id: string}>(
    `/chat/api/events/${eventId}/rooms/create`, { method: 'POST', body: JSON.stringify({ room_name: roomName, member_actor_keys: memberActorKeys, csrf_token: chatCsrfToken }) },
  ),
  chatUpdateRoom: (eventId: number, roomId: string, roomName: string) => requestJson<{ok: true}>(
    `/chat/api/events/${eventId}/rooms/${encoded(roomId)}/update`, { method: 'POST', body: JSON.stringify({ room_name: roomName, csrf_token: chatCsrfToken }) },
  ),
  chatDeleteRoom: (eventId: number, roomId: string) => requestJson<{ok: true}>(
    `/chat/api/events/${eventId}/rooms/${encoded(roomId)}/delete`, { method: 'POST', body: JSON.stringify({ csrf_token: chatCsrfToken }) },
  ),
  chatRoomMembers: (eventId: number, roomId: string) => requestJson<{ok: true; members: ChatRoomMember[]}>(
    `/chat/api/events/${eventId}/rooms/${encoded(roomId)}/members`,
  ),
  chatSetRoomMembers: (eventId: number, roomId: string, memberActorKeys: string[]) => requestJson<{ok: true}>(
    `/chat/api/events/${eventId}/rooms/${encoded(roomId)}/members/set`, { method: 'POST', body: JSON.stringify({ member_actor_keys: memberActorKeys, csrf_token: chatCsrfToken }) },
  ),
  chatRoomMemberCandidates: (eventId: number) => requestJson<{ok: true; candidates: ChatRoomMember[]}>(
    `/chat/api/events/${eventId}/room-member-candidates`,
  ),
  chatMentionCandidates: (eventId: number, roomId: string, query: string) => requestJson<{ok: true; candidates: ChatRoomMember[]}>(
    `/chat/api/events/${eventId}/mentions?room_id=${encoded(roomId)}&q=${encoded(query)}`,
  ),
  chatPresence: (action: 'enter' | 'ping' | 'leave', eventId: number, roomId: string, clientId: string, isVisible = true) => requestJson<{ok: true}>(
    `/chat/api/room-presence/${action}`, { method: 'POST', body: JSON.stringify({ event_id: eventId, room_id: roomId, client_id: clientId, is_visible: isVisible, csrf_token: chatCsrfToken }) },
  ),
  chatPushBootstrap: () => requestJson<{ok: true; csrf_token: string; vapid_public_key: string; sw_url: string}>('/chat/api/push/bootstrap'),
  chatPushSubscribe: (subscription: PushSubscriptionJSON, swScope: string) => requestJson<{ok: true}>(
    '/chat/api/push/subscribe', { method: 'POST', body: JSON.stringify({ ...subscription, sw_scope: swScope, csrf_token: chatCsrfToken }) },
  ),
  chatPushUnsubscribe: (endpoint: string) => requestJson<{ok: true}>(
    '/chat/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint, csrf_token: chatCsrfToken }) },
  ),
  chatDmSnapshot: (dmUuid: string) => requestJson<{ok: true; dm_uuid: string; room_id: string; peer_display_name: string; messages: ChatMessage[]; read_states: ChatReadState[]; csrf_token: string}>(
    `/chat/api/vue/dm/${encoded(dmUuid)}/snapshot`,
  ),
  chatDmOpen: (peerActorKey = '') => requestJson<{ok:true;dm_uuid:string}>(
    '/chat/api/vue/dm/open', { method:'POST', body:JSON.stringify({peer_actor_key:peerActorKey,csrf_token:chatCsrfToken}) },
  ),
  chatDmSettings: (enableUserUser:boolean, adminActorKey:string) => requestJson<{ok:true}>(
    '/chat/api/vue/dm/settings', {method:'POST',body:JSON.stringify({enable_user_user:enableUserUser,admin_actor_key:adminActorKey,csrf_token:chatCsrfToken})},
  ),
  chatDmOlderMessages: (dmUuid: string, beforeId: number, limit = 50) => requestJson<{ok: true; messages: ChatMessage[]; has_more: boolean}>(
    `/chat/api/vue/dm/${encoded(dmUuid)}/messages?before_id=${beforeId}&limit=${limit}`,
  ),
  chatDmSend: (dmUuid: string, body: string) => requestJson<{ok: true; message: ChatMessage}>(
    '/chat/dm/api/send', { method: 'POST', body: JSON.stringify({ dm_uuid: dmUuid, body, csrf_token: chatCsrfToken }) },
  ),
  chatDmEdit: (dmUuid: string, messageId: number, bodyText: string) => requestJson<{ok: true; message: ChatMessage}>(
    `/chat/dm/api/messages/${messageId}/edit`, { method: 'POST', body: JSON.stringify({ dm_uuid: dmUuid, body_text: bodyText, csrf_token: chatCsrfToken }) },
  ),
  chatDmDelete: (dmUuid: string, messageId: number, deleteMode: 'cancel'|'admin' = 'cancel') => requestJson<{ok: true; message: ChatMessage}>(
    `/chat/dm/api/messages/${messageId}/delete`, { method: 'POST', body: JSON.stringify({ dm_uuid: dmUuid, delete_mode: deleteMode, csrf_token: chatCsrfToken }) },
  ),
  chatDmSearch: (dmUuid: string, query: string) => requestJson<{ok: true; results: Array<Record<string, any>>}>(
    `/chat/api/vue/dm/${encoded(dmUuid)}/search?q=${encoded(query)}`,
  ),
  chatDmReactionDetails: (dmUuid: string, messageId: number) => requestJson<{ok: true; groups: Array<{emoji:string;count:number;actors:Array<{actor_key:string;display_name:string}>}>}>(
    `/chat/api/vue/dm/${encoded(dmUuid)}/messages/${messageId}/reactions`,
  ),
  chatDmUploadImages: (dmUuid: string, files: File[], body = '') => {
    const form = new FormData();
    form.append('dm_uuid', dmUuid); form.append('body', body);
    if (chatCsrfToken) form.append('csrf_token', chatCsrfToken);
    files.forEach((file) => form.append('file', file, file.name));
    return requestJson<{ok: true; message: ChatMessage}>('/chat/dm/api/upload-image', { method: 'POST', body: form });
  },
};
