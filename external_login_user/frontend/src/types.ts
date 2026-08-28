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
  actorKind?: 'external' | 'mfu' | null;
  profile?: UserProfile | null;
  mfuUsername?: string | null;
  csrfToken: string;
  navigation: NavigationItem[];
  prerequisites: {
    emailVerificationRequired: boolean;
    privacyAgreementRequired: boolean;
  };
  unread: { notifications: number; chat?: number };
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
}

export interface EventPermissions {
  canView: boolean;
  canOpenChat: boolean;
  canOpenAlbum: boolean;
  canViewMembers: boolean;
  canEditOwnRole: boolean;
  canManageEvent: boolean;
}

export interface EventItem {
  id: number;
  uuid: string;
  title: string;
  startsAt?: string | null;
  placeName?: string | null;
  address?: string | null;
  mapsUrl?: string | null;
  snsHashtag?: string | null;
  googleFormUrl?: string | null;
  lineOpenchatUrl?: string | null;
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
    chat: string;
    album?: string | null;
    members: string;
    payment: string;
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
  urls: { view: string; upload: string };
}

export interface AlbumItem {
  id: string;
  name: string;
  owner?: string;
  accessMode: string;
  eventId?: number | null;
  event?: { id: number; event_uuid?: string; title: string; starts_at?: string | null; place_name?: string | null } | null;
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
  error?: string;
}

export interface ShortcutDownloadJob {
  ok: true;
  job_id: number;
  count: number;
  shortcut_url: string;
  shortcut_status_url: string;
}
