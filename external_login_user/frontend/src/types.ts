export interface RuntimeConfig {
  basePath: string;
  bootstrapUrl: string;
  eventsUrl: string;
  albumApiBase: string;
  loginUrl: string;
}

export interface NavigationItem {
  id: string;
  label: string;
  url: string;
  badge?: 'chat' | 'notifications';
}

export interface UserProfile {
  id: number;
  nickname: string;
  xId?: string | null;
  instagramId?: string | null;
  email?: string | null;
  emailVerified: boolean;
  avatarUrl?: string | null;
}

export interface PortalSession {
  authenticated: boolean;
  actorKind?: 'external' | null;
  profile?: UserProfile | null;
  chatAdminAlias: boolean;
  notificationScope: 'external' | 'mfu';
  csrfToken: string;
  navigation: NavigationItem[];
  prerequisites: {
    profileCompletionRequired: boolean;
    emailVerificationRequired: boolean;
    privacyAgreementRequired: boolean;
  };
  unread: { total: number; notifications: number; chat: number };
  documents: {
    privacyPolicyUrl?: string;
    commerceLawUrl?: string;
    participantTermsUrl?: string;
  };
}

export interface EventMembership {
  id: number;
  status: string;
  isCanceled: boolean;
  paymentStatus: string;
  requirePayment: boolean;
  paidAmountYen?: number | null;
  paidAt?: string | null;
  participantRole: string;
  costumeLabel: string;
  isHost: boolean;
  isSubhost: boolean;
  process: boolean;
  checkinAt?: string | null;
  checkinMethod?: string | null;
  checkinMethodLabel?: string | null;
  receiptUrl?: string | null;
}

export interface EventPermissions {
  canView: boolean;
  canOpenChat: boolean;
  canOpenAlbum: boolean;
  canViewMembers: boolean;
  canOpenPass: boolean;
  canRequestParticipantsPngEmail: boolean;
  canEditOwnRole: boolean;
  canManageEvent: boolean;
}

export interface EventItem {
  id: number;
  uuid: string;
  title: string;
  themeColor: string;
  startsAt?: string | null;
  placeName?: string | null;
  address?: string | null;
  mapsUrl?: string | null;
  snsHashtag?: string | null;
  participantMemo?: string | null;
  googleFormUrl?: string | null;
  lineOpenchatUrl?: string | null;
  lineOpenchatPass?: string | null;
  feeYen?: number | null;
  tipEnabled: boolean;
  payFrom?: string | null;
  payUntil?: string | null;
  albumId?: string | null;
  membership?: EventMembership | null;
  accessRole?: string | null;
  permissions: EventPermissions;
  urls: {
    detail: string;
    chat?: string | null;
    album?: string | null;
    members: string;
    social: string;
    payment: string;
    receipt?: string | null;
    tip: string;
    participantsEmail: string;
    pass?: string | null;
    admin?: string | null;
  };
}

export interface EventMemberItem {
  id: number;
  nickname: string;
  xId?: string | null;
  instagramId?: string | null;
  avatarUrl?: string | null;
  participantRole: string;
  costumeLabel: string;
  isHost: boolean;
  isSubhost: boolean;
  checkinAt?: string | null;
}

export interface ParticipantPass {
  event: {
    uuid: string;
    title: string;
    startsAt?: string | null;
    placeName?: string | null;
    themeColor?: string | null;
  };
  participant: {
    id: number;
    nickname: string;
    avatarUrl?: string | null;
  };
  payment: {
    status: string;
    key: 'paid' | 'unpaid' | 'free';
    label: string;
    amountYen?: number | null;
    paidAt?: string | null;
    receiptUrl?: string | null;
  };
  checkin: {
    checkedIn: boolean;
    at?: string | null;
    method?: 'venue_qr' | null;
    methodLabel?: string | null;
  };
}

export interface AlbumPermissions {
  role: string;
  canView: boolean;
  canUpload: boolean;
  canCreateChild: boolean;
  canRename: boolean;
  canDeleteAlbum: boolean;
  canManageChildren: boolean;
  canChooseChildType?: boolean;
  canManageProcessing: boolean;
  deleteRequiresPasskey: boolean;
}

export interface ChildPermissions {
  canView: boolean;
  canDownload: boolean;
  canUpload: boolean;
  canRenameChild: boolean;
  canDeleteChild: boolean;
  canDeleteMedia: boolean;
  canRenameMedia: boolean;
  createdByCurrentUser: boolean;
  deleteRequiresPasskey: boolean;
}

export interface AlbumChild {
  id: string;
  rowId?: number | string;
  name: string;
  mode: 'normal' | 'process' | 'movie';
  createdAt?: string | null;
  updatedAt?: string | null;
  mediaCount: number;
  mediaUnit: string;
  permissions: ChildPermissions;
  processing?: ProcessingState | null;
  urls: { view: string; upload: string };
}

export interface ProcessingMember {
  user_id: number;
  nickname?: string | null;
  email?: string | null;
  process?: number | boolean;
  requestFlag: boolean;
  completeFlag: boolean;
  updatedAt?: string | null;
}

export interface ProcessingLock {
  username: string;
  acquired_at?: string | null;
  expires_at?: string | null;
  remainingSeconds?: number | null;
  expired?: boolean;
}

export interface ProcessingHistoryItem {
  user?: string | null;
  timestamp?: number | null;
  datetime?: string | null;
}

export interface ProcessingState {
  mode: 'process';
  lock?: ProcessingLock | null;
  history: ProcessingHistoryItem[];
  members: ProcessingMember[];
  currentExternalUserId?: number | null;
  currentUserStatus?: ProcessingMember | null;
  workerName?: string | null;
  currentUserHoldsLock: boolean;
  canUnlock: boolean;
  canForceUnlock: boolean;
  completed: boolean;
}

export interface AlbumItem {
  id: string;
  name: string;
  owner?: string;
  accessMode: string;
  eventId?: number | null;
  event?: { id: number; event_uuid?: string; title: string; starts_at?: string | null; place_name?: string | null; theme_color?: string | null } | null;
  permissions: AlbumPermissions;
  childrenUrl: string;
  accessUrl: string;
}

export interface MediaItem {
  id: string;
  name: string;
  kind: 'image' | 'video';
  size: number;
  modifiedAt?: string;
  capturedAt?: string | null;
  sortSource?: 'exif' | 'filename';
  viewUrl: string;
  downloadUrl: string;
  thumbnailUrl?: string | null;
  posterUrl?: string | null;
  converting?: boolean;
}

export interface Pagination {
  page: number;
  perPage: number;
  total: number;
  pages: number;
  hasNext: boolean;
  hasPrevious: boolean;
}

export interface ApiFailure {
  ok?: false;
  error?: string;
  message?: string;
  joinUrl?: string;
}

export interface AlbumDownloadJob {
  id: string;
  status: string;
  progressUrl?: string;
  downloadUrl?: string | null;
  progress?: number;
  percent?: number;
  total_files?: number;
  processed_files?: number;
  total_bytes?: number;
  processed_bytes?: number;
  error?: string;
}

export interface ShortcutDownloadJob {
  ok: true;
  job_id: number;
  count: number;
  shortcut_url: string;
  shortcut_status_url: string;
}

export interface ChatRoom {
  room_id: string;
  room_name: string;
  is_main: number | boolean;
  unread_count?: number;
  muted_until?: string | null;
}

export interface ChatRoomMember {
  actor_key: string;
  display_name: string;
}

export interface ChatImage {
  seq: number;
  url: string;
  thumb_url: string;
  mime?: string | null;
  size?: number | null;
  width?: number | null;
  height?: number | null;
}

export interface ChatReactionSummary {
  emoji: string;
  count: number;
}

export interface ChatMessage {
  id: number;
  event_id?: number;
  dm_uuid?: string;
  room_id?: string;
  sender_id: string;
  sender_display_name: string;
  sender_avatar_url?: string;
  body?: string;
  body_text?: string;
  body_plain: string;
  editable_text?: string;
  body_html?: string;
  created_at_iso: string;
  created_at_jst_date_label?: string;
  created_at_jst_time_hm?: string;
  is_me: boolean;
  can_edit: boolean;
  can_delete: boolean;
  can_admin_delete?: boolean;
  edited_flag: number;
  deleted_flag: number;
  reply_to_message_id?: number | null;
  thread_root_id?: number | null;
  thread_reply_count?: number;
  reply_to_sender_display_name?: string | null;
  reply_to_body_plain_excerpt?: string | null;
  reactions_summary: ChatReactionSummary[];
  my_reaction?: string | null;
  images: ChatImage[];
}

export interface ChatEventSummary {
  id: number;
  event_uuid: string;
  title: string;
  theme_color?: string | null;
  start_at?: string | null;
  unread_count: number;
}

export interface ChatVueSession {
  ok: true;
  authenticated: boolean;
  actor?: ChatBootstrap['actor'] | null;
  csrf_token: string;
}

export interface ChatDmSummary {
  dm_uuid: string;
  peer_actor_key: string;
  peer_display_name: string;
  last_message: string;
  last_message_at?: string | null;
  unread_count: number;
}

export interface ChatBootstrap {
  actor: { actor_type: string; actor_id: string; display_name: string; actor_key: string; is_chat_admin_alias: boolean };
  csrf_token: string;
  accessible_events: ChatEventSummary[];
  dm_inbox: ChatDmSummary[];
  default_avatar_url: string;
  limits: { message_max_len: number; upload_max_files: number; upload_max_bytes: number };
  reaction_emojis: string[];
  notification_context?: {
    notification_scope: 'external' | 'mfu';
    notification_api_map?: Record<string, string>;
  };
  dm_settings?: { can_manage:boolean; enable_user_user:boolean; admin_actor_key:string };
}

export interface ChatReadState {
  actor_key: string;
  actor_type?: string;
  actor_id?: string;
  display_name: string;
  last_read_message_id: number;
}
