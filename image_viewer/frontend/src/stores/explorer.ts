import { defineStore } from 'pinia';
import { computed, reactive, ref } from 'vue';
import { imageViewerApi } from '@/api/client';
import type { ImageListPayload, MediaItem, SortDirection } from '@/types';

interface FolderData {
  items: MediaItem[];
  page: number;
  pages: number;
  total: number;
  hasMore: boolean;
  loading: boolean;
  version: string;
  offset: number;
  center: number;
}

const emptyData = (): FolderData => ({
  items: [], page: 0, pages: 1, total: 0, hasMore: false, loading: false, version: '', offset: 0, center: 0,
});

const naturalCollator = new Intl.Collator('ja', { numeric: true, sensitivity: 'base' });

function naturalFolders(values: string[]) {
  const folders = Array.from(new Set(['', ...values]));
  const known = new Set(folders);
  const children = new Map<string, string[]>();
  folders.filter(Boolean).forEach((folder) => {
    const parent = folder.split('/').slice(0, -1).join('/');
    const owner = known.has(parent) ? parent : '';
    const entries = children.get(owner) || [];
    entries.push(folder);
    children.set(owner, entries);
  });
  children.forEach((entries) => entries.sort((left, right) =>
    naturalCollator.compare(left.split('/').at(-1) || left, right.split('/').at(-1) || right)));
  const ordered: string[] = [];
  const visited = new Set<string>();
  const walk = (folder: string) => {
    if (visited.has(folder)) return;
    visited.add(folder);
    ordered.push(folder);
    (children.get(folder) || []).forEach(walk);
  };
  walk('');
  folders.filter((folder) => !visited.has(folder)).sort(naturalCollator.compare).forEach(walk);
  return ordered;
}

export const useExplorerStore = defineStore('image-viewer-explorer', () => {
  const folders = ref<string[]>(['']);
  const cache = reactive<Record<string, FolderData>>({});
  const windowCache = new Map<string, ImageListPayload[]>();
  const pendingCenters = new Map<string, number>();

  const keyFor = (folder: string, sort: SortDirection) => `${sort}:${folder}`;
  const dataFor = (folder: string, sort: SortDirection) => cache[keyFor(folder, sort)] ||= emptyData();
  const itemsFor = (folder: string, sort: SortDirection) => dataFor(folder, sort).items;
  const isLoading = (folder: string, sort: SortDirection) => dataFor(folder, sort).loading;
  const hasMore = (folder: string, sort: SortDirection) => dataFor(folder, sort).hasMore;

  function mergePayload(target: FolderData, payload: ImageListPayload, reset: boolean) {
    folders.value = naturalFolders(Array.from(new Set(['', ...(payload.folders || [])])));
    const incoming = payload.images || [];
    if (reset) target.items.splice(0, target.items.length, ...incoming);
    else {
      const merged = new Map(target.items.map((item) => [item.path, item]));
      incoming.forEach((item) => merged.set(item.path, item));
      target.items.splice(0, target.items.length, ...merged.values());
    }
    const pagination = payload.pagination;
    target.page = pagination?.page || 1;
    target.pages = pagination?.pages || 1;
    target.total = pagination?.total ?? target.items.length;
    target.hasMore = Boolean(pagination?.hasMore);
    target.version = String(payload.version || '');
    target.offset = Number(pagination?.offset || 0);
    target.center = Number(pagination?.center ?? (target.offset + Math.floor(target.items.length / 2)));
  }

  function rememberWindow(key: string, payload: ImageListPayload) {
    const windows = (windowCache.get(key) || []).filter((entry) =>
      entry.version === payload.version && entry.pagination?.offset !== payload.pagination?.offset);
    windows.unshift(payload);
    windowCache.set(key, windows.slice(0, 8));
  }

  function cachedWindow(key: string, center: number, version: string) {
    return (windowCache.get(key) || []).find((entry) => {
      if (version && entry.version !== version) return false;
      const start = Number(entry.pagination?.offset || 0);
      return center >= start && center < start + (entry.images?.length || 0);
    });
  }

  function materializeCachedWindows(
    key: string, target: FolderData, center: number, version: string,
  ) {
    const candidates = (windowCache.get(key) || [])
      .filter((entry) => !version || entry.version === version)
      .sort((left, right) => Number(left.pagination?.offset || 0) - Number(right.pagination?.offset || 0));
    if (!candidates.length) return false;

    let pivot = candidates.findIndex((entry) => {
      const start = Number(entry.pagination?.offset || 0);
      return center >= start && center < start + (entry.images?.length || 0);
    });
    if (pivot < 0) return false;

    // Retain overlapping windows on both sides.  Keeping several thousand
    // metadata rows is cheap, while it prevents a 1000-row window replacement
    // from exposing an empty band during fast scrolling.
    let first = pivot;
    let last = pivot;
    while (last - first + 1 < 4) {
      // Prefer the direction of travel (the following range), but keep at
      // least one previous range when possible so a small reverse scroll is
      // equally seamless.
      const canAdvance = last + 1 < candidates.length && (() => {
        const currentEnd = Number(candidates[last].pagination?.offset || 0) + (candidates[last].images?.length || 0);
        return Number(candidates[last + 1].pagination?.offset || 0) <= currentEnd;
      })();
      const canRetreat = first > 0 && (() => {
        const previous = candidates[first - 1];
        const previousEnd = Number(previous.pagination?.offset || 0) + (previous.images?.length || 0);
        return previousEnd >= Number(candidates[first].pagination?.offset || 0);
      })();
      if (canAdvance && (last === pivot || !canRetreat)) last += 1;
      else if (canRetreat) first -= 1;
      else if (canAdvance) last += 1;
      else break;
    }

    const selected = candidates.slice(first, last + 1);
    const offset = Number(selected[0].pagination?.offset || 0);
    const end = Math.max(...selected.map((entry) =>
      Number(entry.pagination?.offset || 0) + (entry.images?.length || 0)));
    const rows = new Array<MediaItem | undefined>(Math.max(0, end - offset));
    selected.forEach((entry) => {
      const start = Number(entry.pagination?.offset || 0) - offset;
      (entry.images || []).forEach((item, index) => { rows[start + index] = item; });
    });
    // Windows deliberately overlap.  If a discontinuity ever appears, keep
    // the known-good single window instead of rendering a false index mapping.
    if (rows.some((item) => !item)) return false;
    const seed = selected[pivot - first] || selected[0];
    mergePayload(target, {
      ...seed,
      images: rows as MediaItem[],
      pagination: {
        ...(seed.pagination || { page: 1, perPage: rows.length, total: rows.length, pages: 1, hasMore: false, offset }),
        offset,
        perPage: rows.length,
        hasMore: end < Number(seed.pagination?.total || end),
        center,
        mode: 'retained-window',
      },
    }, true);
    return true;
  }

  function scheduleAdjacentPrefetch(folder: string, sort: SortDirection, payload: ImageListPayload) {
    const key = keyFor(folder, sort);
    const total = Number(payload.pagination?.total || 0);
    const count = payload.images?.length || 0;
    const offset = Number(payload.pagination?.offset || 0);
    if (!count || !total) return;
    const halfWindow = Math.max(1, Math.floor(count / 2));
    const stride = Math.max(1, count - 50);
    const previousCenter = offset - halfWindow + 50;
    const nextCenter = offset + count + halfWindow - 50;
    const candidates = [
      previousCenter,
      previousCenter - stride,
      nextCenter,
      nextCenter + stride,
    ].filter((value) => value >= 0 && value < total)
      .map((value) => Math.max(0, Math.min(Math.max(0, total - 1), value)));
    candidates.forEach((value) => {
      if (cachedWindow(key, value, String(payload.version || ''))) return;
      imageViewerApi.list(folder, sort, 1, 1000, value)
        .then((next) => {
          rememberWindow(key, next);
          const target = dataFor(folder, sort);
          // Grow the currently visible retained range in the background.  This
          // is intentionally silent: no loading overlay and no scroll reset.
          materializeCachedWindows(key, target, target.center, target.version);
        })
        .catch(() => {});
    });
  }

  async function fetchWindow(folder: string, sort: SortDirection, center: number) {
    const key = keyFor(folder, sort);
    const target = dataFor(folder, sort);
    const remembered = cachedWindow(key, center, target.version);
    if (remembered) {
      if (!materializeCachedWindows(key, target, center, target.version)) {
        mergePayload(target, remembered, true);
      }
      scheduleAdjacentPrefetch(folder, sort, remembered);
      return;
    }
    const payload = await imageViewerApi.list(folder, sort, 1, 1000, center);
    rememberWindow(key, payload);
    if (!materializeCachedWindows(key, target, center, String(payload.version || ''))) {
      mergePayload(target, payload, true);
    }
    scheduleAdjacentPrefetch(folder, sort, payload);
  }

  async function load(folder: string, sort: SortDirection, reset = false) {
    const target = dataFor(folder, sort);
    if (target.loading) return;
    target.loading = true;
    try {
      const center = reset ? 0 : target.center;
      const payload = await imageViewerApi.list(folder, sort, 1, 1000, center);
      rememberWindow(keyFor(folder, sort), payload);
      mergePayload(target, payload, true);
      scheduleAdjacentPrefetch(folder, sort, payload);
    } finally {
      target.loading = false;
    }
  }

  async function loadMore(folder: string, sort: SortDirection) {
    const target = dataFor(folder, sort);
    if (target.loading || !target.hasMore) return;
    target.loading = true;
    try {
      const nextPage = Math.min(target.pages, Math.max(1, target.page + 1));
      mergePayload(target, await imageViewerApi.list(folder, sort, nextPage), false);
    } finally {
      target.loading = false;
    }
  }

  async function loadCenter(folder: string, sort: SortDirection, center: number) {
    const key = keyFor(folder, sort);
    const target = dataFor(folder, sort);
    const edgeMargin = Math.min(450, Math.max(120, Math.floor(target.items.length * 0.20)));
    const loadedEnd = target.offset + target.items.length;
    if (center >= target.offset + edgeMargin && center < loadedEnd - edgeMargin) return;
    let wanted = center;
    if (center >= loadedEnd - edgeMargin && loadedEnd < target.total) {
      wanted = Math.min(target.total - 1, loadedEnd + 250);
    } else if (center < target.offset + edgeMargin && target.offset > 0) {
      wanted = Math.max(0, target.offset - 250);
    }
    pendingCenters.set(key, wanted);
    if (target.loading) return;
    target.loading = true;
    try {
      while (pendingCenters.has(key)) {
        const wanted = pendingCenters.get(key)!;
        pendingCenters.delete(key);
        await fetchWindow(folder, sort, wanted);
      }
    } finally {
      target.loading = false;
    }
  }

  async function refreshFolder(folder: string) {
    const matching = Object.keys(cache).filter((key) => key.endsWith(`:${folder}`));
    await Promise.all(matching.map(async (key) => {
      const sort = key.startsWith('desc:') ? 'desc' : 'asc';
      const target = dataFor(folder, sort);
      const center = target.center;
      windowCache.delete(key);
      await fetchWindow(folder, sort, center);
    }));
  }

  function clearFolder(folder: string) {
    Object.keys(cache).filter((key) => key.endsWith(`:${folder}`)).forEach((key) => delete cache[key]);
  }

  const allFolders = computed(() => folders.value);
  return { allFolders, dataFor, itemsFor, isLoading, hasMore, load, loadMore, loadCenter, refreshFolder, clearFolder };
});
