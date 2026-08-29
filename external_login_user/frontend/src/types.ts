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
  startsAt?: string | null;
  placeName?: string | null;
  address?: string | null;
  mapsUrl?: string | null;
  snsHashtag?: string | null;
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
    chat: string;
    album?: string | null;
    members: string;
    social: string;
    payment: string;
    receipt?: string | null;
    tip: string;
    participantsEmail: string;
    pass?: string | null;
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
  error?: string;
}

export interface ShortcutDownloadJob {
  ok: true;
  job_id: number;
  count: number;
  shortcut_url: string;
  shortcut_status_url: string;
}
