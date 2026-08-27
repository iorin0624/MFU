export type SortDirection = 'asc' | 'desc';
export type ViewSize = 'xxl' | 'xl' | 'lg';
export type WindowKind = 'explorer' | 'image' | 'video';

export interface MediaItem {
  id?: number;
  uuid?: string;
  name: string;
  path: string;
  folder: string;
  mediaType: 'image' | 'video';
  size: number;
  mtime: number;
  url: string;
  thumbUrl?: string | null;
  hasThumb?: boolean;
  sourceUrl?: string;
}

export interface Pagination {
  page: number;
  perPage: number;
  total: number;
  pages: number;
  hasMore: boolean;
  offset: number;
  center?: number;
  mode?: string;
}

export interface ImageListPayload {
  ok: boolean;
  catalog?: boolean;
  folders: string[];
  images: MediaItem[];
  folder?: string;
  version?: string;
  pagination?: Pagination;
}

export interface RuntimeConfig {
  legacyUrl: string;
  imagesUrl: string;
  imagesVersionUrl: string;
  createFolderUrl: string;
  propertiesUrl: string;
  renameUrl: string;
  appendSequenceUrl: string;
  deleteUrl: string;
  moveUrl: string;
  copyUrl: string;
  uploadUrl: string;
  pasteUrl: string;
  thumbnailUrl: string;
  thumbnailJobUrl: string;
  isAdmin: boolean;
}

export interface ExplorerWindowState {
  folder: string;
  sort: SortDirection;
  viewSize: ViewSize;
  selectedPaths: string[];
  anchorPath: string;
  numbering: boolean;
  appendSources: string[];
}

export interface DesktopWindow {
  id: string;
  kind: WindowKind;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  z: number;
  minimized: boolean;
  maximized: boolean;
  explorer?: ExplorerWindowState;
  media?: MediaItem;
  sequence?: MediaItem[];
  sequenceContext?: {
    folder: string;
    sort: SortDirection;
    offset: number;
    total: number;
  };
}

export interface ApiErrorPayload {
  ok?: boolean;
  error?: string;
  message?: string;
  action?: string;
}
