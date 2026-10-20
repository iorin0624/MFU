<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';

type PublicFile = {
  id: number;
  name: string;
  hidden: boolean;
  flagged: boolean;
  kind: 'image' | 'video' | 'file';
  url: string;
  thumbnailUrl: string | null;
  relativePath: string;
  mobileDownload: boolean;
  capturedAt: string | null;
};

type ReplyImage = { name: string; url: string };
type PreparedZip = {
  key: string;
  url: string;
  name: string;
  count: number;
  size: number;
  preparedAt: number;
  expiresInSeconds: number;
};
type NoticePart = { kind: 'text' | 'link'; value: string };
type ReplyGroup = {
  id: number;
  replyUuid: string;
  postedAt: string;
  count: number;
  images: ReplyImage[];
};

type ViewerPayload = {
  ok: boolean;
  upload: {
    uuid: string;
    title: string;
    date: string;
    expireAt: string;
    modeLabel: string;
    generateThumbnails: boolean;
  };
  permissions: { manageVisibility: boolean };
  counts: { public: number; hidden: number; total: number };
  notice: string;
  reply: {
    enabled: boolean;
    uploadUrl: string;
    canList: boolean;
    groups: ReplyGroup[];
  };
  replyOnly: boolean;
  download: { zipUrl: string; historyUrl: string | null; mobileEnabled: boolean };
  files: PublicFile[];
};

declare global {
  interface Window {
    MFUZipDownload?: {
      prepare(options: {
        paths: string[];
        csrfToken: string;
        key: string;
        sequenceRename?: boolean;
        onProgress: (data: Record<string, number>) => void;
      }): Promise<{ key: string; download_url: string; download_name: string; size: number; expires_in_seconds: number }>;
    };
    MFUShortcutDownload?: {
      isIOSDevice: () => boolean;
      loadConfig: (force?: boolean) => Promise<{ enabled: boolean }>;
      launch: (job: unknown) => Promise<unknown>;
    };
  }
}

const configElement = document.getElementById('public-upload-config');
const config = JSON.parse(configElement?.textContent || '{}') as { uuid?: string; replyOnly?: boolean };
const requestedSection = new URLSearchParams(window.location.search).get('section')
  || window.location.hash.replace(/^#/, '');
const focusReplyOnLoad = requestedSection === 'reply';
let csrfToken = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
const filenameCollator = new Intl.Collator('ja', { numeric: true, sensitivity: 'base' });
const data = ref<ViewerPayload | null>(null);
const loading = ref(true);
const error = ref('');
const selected = ref<number[]>([]);
const noticeOpen = ref(true);
const replyDetails = ref<HTMLDetailsElement | null>(null);
const appendDetails = ref<HTMLDetailsElement | null>(null);
const albumPanel = ref<HTMLElement | null>(null);
const filter = ref<'all' | 'public' | 'hidden'>('all');
const managing = ref(false);
const busy = ref(false);
const toast = ref('');
const progress = ref<number | null>(null);
const progressText = ref('');
const preparedZip = ref<PreparedZip | null>(null);
const lightboxIndex = ref(-1);
const replyFiles = ref<File[]>([]);
const replyComment = ref('');
const replyPreviewUrls = ref<string[]>([]);
const replyBusy = ref(false);
const replyProgress = ref<number | null>(null);
const replyError = ref('');
const replyNeedsReauth = ref(false);
const replyAuthUrl = ref('');
const replyLightboxImages = ref<ReplyImage[]>([]);
const replyLightboxIndex = ref(-1);
const appendFiles = ref<File[]>([]);
const appendBusy = ref(false);
const appendProgress = ref<number | null>(null);
const appendProgressText = ref('');
const appendResult = ref('');
const appendErrors = ref<string[]>([]);
const appendRenames = ref<string[]>([]);
let lightboxTouchStart: { x: number; y: number; at: number } | null = null;
let activeLoadController: AbortController | null = null;
let loadSequence = 0;
let activeNoticeFingerprint = '';

const filteredFiles = computed(() => {
  const files = data.value?.files || [];
  if (!managing.value || filter.value === 'all') return files;
  return files.filter((file) => filter.value === 'hidden' ? file.hidden : !file.hidden);
});
const displayedFiles = computed(() => filteredFiles.value);
const selectableFiles = computed(() => (data.value?.files || []).filter((file) => !file.hidden));
const selectedFiles = computed(() => (data.value?.files || []).filter((file) => selected.value.includes(file.id)));
const selectedImageIds = computed(() => selectedFiles.value.filter((file) => file.kind === 'image').map((file) => file.id));
const lightboxFiles = computed(() => displayedFiles.value.filter((file) => file.kind === 'image' || file.kind === 'video'));
const lightboxFile = computed(() => lightboxFiles.value[lightboxIndex.value] || null);
const replyLightboxImage = computed(() => replyLightboxImages.value[replyLightboxIndex.value] || null);
const noticeParts = computed<NoticePart[]>(() => {
  const notice = data.value?.notice || '';
  const parts: NoticePart[] = [];
  const urlPattern = /https?:\/\/[^\s<>"']+/giu;
  let cursor = 0;
  for (const match of notice.matchAll(urlPattern)) {
    const index = match.index ?? cursor;
    if (index > cursor) parts.push({ kind: 'text', value: notice.slice(cursor, index) });

    const matchedValue = match[0];
    const trailing = matchedValue.match(/[.,!?;:。、，．！？；：）」』】〕〉》]+$/u)?.[0] || '';
    const url = trailing ? matchedValue.slice(0, -trailing.length) : matchedValue;
    if (url) parts.push({ kind: 'link', value: url });
    if (trailing) parts.push({ kind: 'text', value: trailing });
    cursor = index + matchedValue.length;
  }
  if (cursor < notice.length) parts.push({ kind: 'text', value: notice.slice(cursor) });
  return parts;
});
const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

function formatDate(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');
  return match ? `${match[1]}年${match[2]}月${match[3]}日` : value;
}

function formatDateTime(value: string) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(parsed);
}

function sortFilesByCaptureTime(files: PublicFile[]) {
  return [...files].sort((left, right) => (
    Number(!left.capturedAt) - Number(!right.capturedAt)
    || String(left.capturedAt || '').localeCompare(String(right.capturedAt || ''))
    || filenameCollator.compare(left.name, right.name)
    || left.id - right.id
  ));
}

function noticeFingerprint(value: string) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `${value.length}-${(hash >>> 0).toString(16)}`;
}

function noticeStorageKey() {
  return `mfu-public-notice-collapsed:${config.uuid || 'unknown'}`;
}

function applyNoticeInitialState(notice: string) {
  const fingerprint = noticeFingerprint(notice);
  if (fingerprint === activeNoticeFingerprint) return;
  activeNoticeFingerprint = fingerprint;
  try {
    noticeOpen.value = localStorage.getItem(noticeStorageKey()) !== fingerprint;
  } catch {
    noticeOpen.value = true;
  }
}

async function scrollToAlbumStart() {
  await nextTick();
  const target = albumPanel.value;
  if (!target) return;
  window.requestAnimationFrame(() => {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function closeNotice() {
  noticeOpen.value = false;
  void scrollToAlbumStart();
}

function keepNoticeCollapsed() {
  try {
    localStorage.setItem(noticeStorageKey(), activeNoticeFingerprint);
  } catch {
    // Storage can be unavailable in private browsing; closing still works.
  }
  noticeOpen.value = false;
  void scrollToAlbumStart();
}

async function focusRequestedSection() {
  await nextTick();
  const target = focusReplyOnLoad
    ? replyDetails.value
    : document.getElementById(requestedSection);
  if (!target) return;

  let collapsible: HTMLDetailsElement | null = target instanceof HTMLDetailsElement
    ? target
    : target.closest('details');
  while (collapsible) {
    collapsible.open = true;
    collapsible = collapsible.parentElement?.closest('details') || null;
  }

  const scroll = () => target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  window.requestAnimationFrame(() => window.requestAnimationFrame(scroll));
  window.setTimeout(scroll, 350);
}

async function load() {
  const sequence = ++loadSequence;
  activeLoadController?.abort();
  const controller = new AbortController();
  activeLoadController = controller;
  const timeout = window.setTimeout(() => controller.abort(), 20_000);
  loading.value = true;
  error.value = '';
  try {
    const response = await fetch(`/view/${encodeURIComponent(config.uuid || '')}/api`, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => null) as ViewerPayload | null;
    if (!response.ok || !payload?.ok) throw new Error((payload as { message?: string } | null)?.message || '表示情報を取得できませんでした。');
    payload.files = sortFilesByCaptureTime(payload.files);
    data.value = payload;
    if (payload.notice) applyNoticeInitialState(payload.notice);
    selected.value = selected.value.filter((id) => payload.files.some((file) => file.id === id && !file.hidden));
    await focusRequestedSection();
  } catch (reason) {
    if (sequence !== loadSequence) return;
    error.value = reason instanceof DOMException && reason.name === 'AbortError'
      ? '通信が時間内に完了しませんでした。再読み込みしてください。'
      : reason instanceof Error ? reason.message : '表示情報を取得できませんでした。';
  } finally {
    window.clearTimeout(timeout);
    if (sequence === loadSequence) {
      loading.value = false;
      activeLoadController = null;
    }
  }
}

function toggleSelection(file: PublicFile) {
  if (file.hidden && !managing.value) return;
  selected.value = selected.value.includes(file.id)
    ? selected.value.filter((id) => id !== file.id)
    : [...selected.value, file.id];
}

function selectAll() {
  const targets = managing.value ? displayedFiles.value : selectableFiles.value;
  selected.value = Array.from(new Set([...selected.value, ...targets.map((file) => file.id)]));
}

function clearSelection() { selected.value = []; }

function showToast(message: string) {
  toast.value = message;
  window.setTimeout(() => { if (toast.value === message) toast.value = ''; }, 3000);
}

function openLightbox(file: PublicFile) {
  const index = lightboxFiles.value.findIndex((candidate) => candidate.id === file.id);
  if (index >= 0) lightboxIndex.value = index;
}

function closeLightbox() { lightboxIndex.value = -1; }
function moveLightbox(delta: number) {
  if (!lightboxFiles.value.length) return;
  lightboxIndex.value = (lightboxIndex.value + delta + lightboxFiles.value.length) % lightboxFiles.value.length;
}

function keydown(event: KeyboardEvent) {
  if (replyLightboxIndex.value >= 0) {
    if (event.key === 'Escape') closeReplyLightbox();
    if (event.key === 'ArrowLeft') moveReplyLightbox(-1);
    if (event.key === 'ArrowRight') moveReplyLightbox(1);
    return;
  }
  if (lightboxIndex.value < 0) return;
  if (event.key === 'Escape') closeLightbox();
  if (event.key === 'ArrowLeft') moveLightbox(-1);
  if (event.key === 'ArrowRight') moveLightbox(1);
  if (
    event.key.toLowerCase() === 'x'
    && data.value?.permissions.manageVisibility
    && lightboxFile.value
  ) {
    event.preventDefault();
    void changeLightboxVisibility(lightboxFile.value);
  }
  if (
    event.key.toLowerCase() === 'e'
    && !event.repeat
    && data.value?.permissions.manageVisibility
    && lightboxFile.value?.kind === 'image'
  ) {
    event.preventDefault();
    void changeLightboxFlag(lightboxFile.value);
  }
}

function startLightboxSwipe(event: TouchEvent) {
  if (event.touches.length !== 1) {
    lightboxTouchStart = null;
    return;
  }
  const touch = event.touches[0];
  lightboxTouchStart = { x: touch.clientX, y: touch.clientY, at: Date.now() };
}

function finishLightboxSwipe(event: TouchEvent) {
  const start = lightboxTouchStart;
  lightboxTouchStart = null;
  if (!start || event.changedTouches.length !== 1) return;
  const touch = event.changedTouches[0];
  const deltaX = touch.clientX - start.x;
  const deltaY = touch.clientY - start.y;
  if (Date.now() - start.at > 800 || Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.2) return;
  moveLightbox(deltaX < 0 ? 1 : -1);
}

async function createShortcutJob(paths: string[]) {
  const response = await fetch('/mobile-download/api/jobs', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ upload_uuid: data.value?.upload.uuid, paths }),
  });
  const payload = await response.json().catch(() => null) as Record<string, unknown> | null;
  if (!response.ok || !payload?.ok) throw new Error(String(payload?.error || '準備に失敗しました。'));
  return payload;
}

async function shortcutDownload() {
  if (busy.value || !window.MFUShortcutDownload) return;
  const paths = selectedFiles.value.filter((file) => file.mobileDownload).map((file) => file.relativePath);
  if (!paths.length) return showToast('ショートカットで保存できる写真を選択してください。');
  busy.value = true;
  try {
    await window.MFUShortcutDownload.launch(await createShortcutJob(paths));
  } catch (reason) {
    showToast(reason instanceof Error ? reason.message : 'ショートカットを起動できませんでした。');
  } finally { busy.value = false; }
}

async function zipDownload() {
  if (busy.value || !data.value || !selectedFiles.value.length) return;
  const paths = selectedFiles.value.map((file) => file.relativePath);
  if (!window.MFUZipDownload) return showToast('ZIPダウンロードを開始できませんでした。');
  busy.value = true;
  progress.value = 0;
  progressText.value = 'ZIPを準備しています';
  try {
    const result = await window.MFUZipDownload.prepare({
      paths,
      csrfToken,
      key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      sequenceRename: true,
      onProgress: (status) => {
        progress.value = Number(status.percent || 0);
        const done = Number(status.processed_files || 0);
        const total = Number(status.total_files || 0);
        progressText.value = `${progress.value}%（${done}/${total}件）`;
      },
    });
    if (!/^\/api\/zip-download\/[0-9A-Za-z._:-]+$/.test(result.download_url)) {
      throw new Error('無効なダウンロードURLです。');
    }
    preparedZip.value = {
      key: result.key,
      url: result.download_url,
      name: result.download_name,
      count: paths.length,
      size: result.size,
      preparedAt: Date.now(),
      expiresInSeconds: result.expires_in_seconds,
    };
    progress.value = 100;
    progressText.value = '生成完了';
    showToast('ZIPの準備ができました。「ZIPをダウンロード」を押してください。');
  } catch (reason) {
    showToast(reason instanceof Error ? `ZIPの生成に失敗しました: ${reason.message}` : 'ZIPの生成に失敗しました。');
  } finally {
    busy.value = false;
    window.setTimeout(() => { progress.value = null; }, 1500);
  }
}

async function changeVisibility(hidden: boolean) {
  if (busy.value || !selected.value.length || !data.value) return;
  busy.value = true;
  try {
    const response = await fetch(`/view/${encodeURIComponent(data.value.upload.uuid)}/visibility`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ file_ids: selected.value, hidden }),
    });
    const payload = await response.json().catch(() => null) as { message?: string } | null;
    if (!response.ok) throw new Error(payload?.message || '公開状態を変更できませんでした。');
    clearSelection();
    await load();
    showToast(hidden ? '非公開に変更しました。' : '公開に戻しました。');
  } catch (reason) {
    showToast(reason instanceof Error ? reason.message : '公開状態を変更できませんでした。');
  } finally { busy.value = false; }
}

async function changeLightboxVisibility(file: PublicFile, forceHidden?: boolean) {
  if (busy.value || !data.value?.permissions.manageVisibility) return;
  const hidden = typeof forceHidden === 'boolean' ? forceHidden : !file.hidden;
  if (hidden === file.hidden) return;
  const fileId = file.id;
  busy.value = true;
  try {
    const response = await fetch(`/view/${encodeURIComponent(data.value.upload.uuid)}/visibility`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ file_ids: [fileId], hidden }),
    });
    const payload = await response.json().catch(() => null) as { message?: string } | null;
    if (!response.ok) throw new Error(payload?.message || '公開状態を変更できませんでした。');
    await load();
    const newIndex = lightboxFiles.value.findIndex((candidate) => candidate.id === fileId);
    if (newIndex >= 0) lightboxIndex.value = newIndex;
    else closeLightbox();
    showToast(hidden ? '非公開に変更しました。' : '公開に戻しました。');
  } catch (reason) {
    showToast(reason instanceof Error ? reason.message : '公開状態を変更できませんでした。');
  } finally { busy.value = false; }
}

async function updateFlags(fileIds: number[], flagged: boolean, lightboxFileId?: number) {
  if (busy.value || !data.value || !fileIds.length) return;
  busy.value = true;
  try {
    const response = await fetch(`/view/${encodeURIComponent(data.value.upload.uuid)}/flag`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ file_ids: fileIds, flagged }),
    });
    const payload = await response.json().catch(() => null) as { message?: string } | null;
    if (!response.ok) throw new Error(payload?.message || 'フラグを変更できませんでした。');
    if (lightboxFileId === undefined) clearSelection();
    await load();
    if (lightboxFileId !== undefined) {
      const index = lightboxFiles.value.findIndex((candidate) => candidate.id === lightboxFileId);
      if (index >= 0) lightboxIndex.value = index;
      else closeLightbox();
    }
    showToast(flagged ? '加工バック希望のフラグを付けました。' : 'フラグを外しました。');
  } catch (reason) {
    showToast(reason instanceof Error ? reason.message : 'フラグを変更できませんでした。');
  } finally { busy.value = false; }
}

function changeFlags(flagged: boolean) {
  void updateFlags(selectedImageIds.value, flagged);
}

function changeLightboxFlag(file: PublicFile) {
  if (file.kind !== 'image') return;
  void updateFlags([file.id], !file.flagged, file.id);
}

function setAppendFiles(files: File[]) {
  if (appendBusy.value) return;
  appendFiles.value = files;
  appendResult.value = '';
  appendErrors.value = [];
  appendRenames.value = [];
  appendProgress.value = null;
}

function chooseAppendFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  setAppendFiles(Array.from(input.files || []));
  input.value = '';
}

function dropAppendFiles(event: DragEvent) {
  setAppendFiles(Array.from(event.dataTransfer?.files || []));
}

function appendOneFile(uuid: string, file: File, onProgress: (loaded: number) => void) {
  return new Promise<{ saved: string; thumbnailStatus: string }>((resolve, reject) => {
    const form = new FormData();
    form.append('photo', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/view/${encodeURIComponent(uuid)}/append`);
    xhr.responseType = 'json';
    xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded);
    };
    xhr.onload = () => {
      const payload = xhr.response as { ok?: boolean; saved?: string; thumbnailStatus?: string; message?: string } | null;
      if (xhr.status >= 200 && xhr.status < 300 && payload?.ok && payload.saved) {
        onProgress(file.size);
        resolve({ saved: payload.saved, thumbnailStatus: payload.thumbnailStatus || '' });
      } else {
        const failure = new Error(payload?.message || `アップロードに失敗しました（HTTP ${xhr.status}）。`);
        reject(Object.assign(failure, { status: xhr.status }));
      }
    };
    xhr.onerror = () => reject(new Error('通信に失敗しました。'));
    xhr.send(form);
  });
}

async function appendUploadFiles() {
  if (appendBusy.value || !data.value?.permissions.manageVisibility || !appendFiles.value.length) return;
  const files = [...appendFiles.value];
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const uuid = data.value.upload.uuid;
  const failed: File[] = [];
  const errors: string[] = [];
  const renames: string[] = [];
  let completedBytes = 0;
  let saved = 0;
  let thumbnailErrors = 0;
  appendBusy.value = true;
  appendProgress.value = 0;
  appendResult.value = '';
  appendErrors.value = [];
  appendRenames.value = [];
  try {
    for (const [index, file] of files.entries()) {
      appendProgressText.value = `アップロード中 ${index + 1}/${files.length}：${file.name}`;
      try {
        const result = await appendOneFile(uuid, file, (loaded) => {
          appendProgress.value = totalBytes
            ? Math.min(99, Math.round(((completedBytes + loaded) / totalBytes) * 100))
            : Math.round(((index + 1) / files.length) * 99);
        });
        saved += 1;
        if (result.saved !== file.name) renames.push(`${file.name} → ${result.saved}`);
        if (result.thumbnailStatus === 'error') thumbnailErrors += 1;
      } catch (reason) {
        failed.push(file);
        errors.push(`${file.name}: ${reason instanceof Error ? reason.message : '保存に失敗しました。'}`);
        const status = (reason as { status?: number })?.status;
        if (status === 401 || status === 403 || status === 410) {
          failed.push(...files.slice(index + 1));
          break;
        }
      }
      completedBytes += file.size;
    }
    appendFiles.value = failed;
    appendErrors.value = errors;
    appendRenames.value = renames;
    appendResult.value = saved
      ? `${saved}件を追加しました。${failed.length ? ` 未保存 ${failed.length}件は選択欄に残しています。` : ''}`
      : '追加できたファイルはありません。';
    if (thumbnailErrors) appendErrors.value.push(`${thumbnailErrors}件のサムネイル生成を開始できませんでした。`);
    if (saved) {
      await load();
      await nextTick();
      if (appendDetails.value) appendDetails.value.open = true;
    }
  } finally {
    appendBusy.value = false;
    appendProgress.value = null;
    appendProgressText.value = '';
  }
}

function clickPreparedZip(event: MouseEvent) {
  const zip = preparedZip.value;
  if (!zip) return;
  if (zip.expiresInSeconds > 0 && Date.now() - zip.preparedAt >= zip.expiresInSeconds * 1000) {
    event.preventDefault();
    showToast('ZIPの有効期限が切れました。「ZIPでDL」から作り直してください。');
    return;
  }
  const intentRef = crypto.randomUUID();
  const url = new URL(zip.url, window.location.origin);
  url.searchParams.set('intent', intentRef);
  (event.currentTarget as HTMLAnchorElement).href = url.toString();
  // The click audit must never delay or block the browser's native download.
  void fetch('/api/zip-download-intent', {
    method: 'POST',
    credentials: 'same-origin',
    keepalive: true,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ key: zip.key, intent_ref: intentRef }),
  }).catch(() => {});
}

function startManaging() {
  managing.value = true;
  filter.value = 'all';
  clearSelection();
}
function stopManaging() {
  managing.value = false;
  filter.value = 'all';
  clearSelection();
}

function setReplyFiles(files: File[]) {
  replyPreviewUrls.value.forEach((url) => URL.revokeObjectURL(url));
  replyFiles.value = files.filter((file) => file.type.startsWith('image/') || /\.(heic|heif)$/i.test(file.name));
  replyPreviewUrls.value = replyFiles.value.map((file) => URL.createObjectURL(file));
  replyError.value = '';
  replyNeedsReauth.value = false;
}

function chooseReplyFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  setReplyFiles(Array.from(input.files || []));
  input.value = '';
}

function dropReplyFiles(event: DragEvent) {
  setReplyFiles(Array.from(event.dataTransfer?.files || []));
}

class ReplyCsrfError extends Error {}
class ReplyAuthError extends Error {}

async function refreshReplySession() {
  const uuid = data.value?.upload.uuid;
  if (!uuid) throw new Error('アップロード先を確認できませんでした。');
  const response = await fetch(`/view/${encodeURIComponent(uuid)}/replies/session`, {
    credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
  });
  const payload = await response.json().catch(() => null) as {
    ok?: boolean; authorized?: boolean; csrfToken?: string; authUrl?: string; message?: string;
  } | null;
  if (!response.ok || !payload?.ok || !payload.csrfToken) {
    throw new Error(payload?.message || '認証状態を確認できませんでした。時間をおいて再送してください。');
  }
  csrfToken = payload.csrfToken;
  replyAuthUrl.value = payload.authUrl || '';
  if (!payload.authorized) {
    throw new ReplyAuthError(replyAuthUrl.value
      ? '認証状態が切れました。新しいタブで再認証し、この画面に戻って再送してください。'
      : '認証状態が切れました。最初に案内された共有リンクを別タブで開き直し、この画面に戻って再送してください。');
  }
  replyNeedsReauth.value = false;
}

function sendReply(form: FormData): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', data.value!.reply.uploadUrl);
    xhr.responseType = 'json';
    xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) replyProgress.value = Math.round((event.loaded / event.total) * 100);
    };
    xhr.onload = () => {
      const payload = xhr.response as { error?: string; message?: string } | null;
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else if ((xhr.status === 400 || xhr.status === 403) && payload?.error === 'csrf_failed') {
        reject(new ReplyCsrfError(payload.message || '認証状態が切れました。'));
      } else if (xhr.status === 403 && payload?.error === 'auth_required') {
        reject(new ReplyAuthError(payload.message || '認証状態が切れました。'));
      } else reject(new Error(payload?.message || '折り返しを送信できませんでした。'));
    };
    xhr.onerror = () => reject(new Error('通信に失敗しました。保存済みか確認してから再送してください。'));
    xhr.send(form);
  });
}

async function submitReply() {
  if (replyBusy.value || !data.value?.reply.enabled) return;
  if (!replyFiles.value.length) {
    replyError.value = '写真を選択してください。';
    return;
  }
  replyBusy.value = true;
  replyError.value = '';
  replyProgress.value = 0;
  const form = new FormData();
  replyFiles.value.forEach((file) => form.append('photos', file, file.name));
  form.append('comment', replyComment.value);
  try {
    await refreshReplySession();
    try {
      await sendReply(form);
    } catch (reason) {
      if (!(reason instanceof ReplyCsrfError)) throw reason;
      // CSRF rejection happens before saving; only this case is safe to retry.
      await refreshReplySession();
      replyProgress.value = 0;
      await sendReply(form);
    }
    setReplyFiles([]);
    replyComment.value = '';
    await load();
    showToast('折り返しを送信しました。');
  } catch (reason) {
    if (reason instanceof ReplyAuthError || reason instanceof ReplyCsrfError) replyNeedsReauth.value = true;
    replyError.value = reason instanceof Error ? reason.message : '折り返しを送信できませんでした。';
  } finally {
    replyBusy.value = false;
    window.setTimeout(() => { replyProgress.value = null; }, 800);
  }
}

function openReplyLightbox(group: ReplyGroup, index: number) {
  replyLightboxImages.value = group.images;
  replyLightboxIndex.value = index;
}
function closeReplyLightbox() { replyLightboxIndex.value = -1; replyLightboxImages.value = []; }
function moveReplyLightbox(delta: number) {
  if (!replyLightboxImages.value.length) return;
  replyLightboxIndex.value = (replyLightboxIndex.value + delta + replyLightboxImages.value.length) % replyLightboxImages.value.length;
}

onMounted(() => {
  window.addEventListener('keydown', keydown);
  void load();
});
onUnmounted(() => {
  activeLoadController?.abort();
  window.removeEventListener('keydown', keydown);
  replyPreviewUrls.value.forEach((url) => URL.revokeObjectURL(url));
});
</script>

<template>
  <main class="public-viewer">
    <div v-if="loading" class="state-card">読み込んでいます…</div>
    <div v-else-if="error" class="state-card error-state">
      <strong>読み込みに失敗しました</strong><span>{{ error }}</span>
      <button type="button" @click="load">再読み込み</button>
    </div>
    <template v-else-if="data">
      <header class="viewer-header">
        <div>
          <span class="eyebrow">FILE UPLOAD</span>
          <h1>{{ data.upload.title }}</h1>
          <dl class="metadata">
            <div><dt>撮影日</dt><dd>{{ formatDate(data.upload.date) || '未設定' }}</dd></div>
            <div><dt>枚数</dt><dd>{{ data.counts.total }}枚</dd></div>
            <div><dt>保存期間</dt><dd>{{ formatDate(data.upload.expireAt) || '未設定' }}<template v-if="data.upload.expireAt"> 23:59</template></dd></div>
          </dl>
        </div>
        <a v-if="data.download.historyUrl" class="outline-button" :href="data.download.historyUrl">ダウンロード履歴</a>
      </header>

      <section v-if="data.notice" class="notice-grid">
        <details v-if="data.notice" class="notice-card" :open="noticeOpen" @toggle="noticeOpen=($event.currentTarget as HTMLDetailsElement).open">
          <summary><div class="notice-summary-copy"><h2 class="notice-alert-title">⚠️お知らせ⚠️</h2><small>必ずお読みください</small></div><span aria-hidden="true">⌄</span></summary>
          <p><template v-for="(part, index) in noticeParts" :key="`${index}-${part.value}`"><a v-if="part.kind === 'link'" :href="part.value" target="_blank" rel="noopener noreferrer">{{ part.value }}</a><template v-else>{{ part.value }}</template></template></p>
          <div class="notice-actions">
            <button type="button" @click="closeNotice">閉じる</button>
            <button type="button" class="notice-remember-button" @click="keepNoticeCollapsed">以後折りたたむ</button>
          </div>
        </details>
      </section>

      <details v-if="data.reply.enabled" id="reply" ref="replyDetails" class="reply-upload-panel reply-collapsible" :open="focusReplyOnLoad">
        <summary class="reply-section-heading">
          <div><h2>折り返し</h2><p>加工済みの写真を選択して送信できます。</p></div><span aria-hidden="true">⌄</span>
        </summary>
        <form class="reply-upload-form" @submit.prevent="submitReply">
          <label class="reply-drop-zone" @dragover.prevent @drop.prevent="dropReplyFiles">
            <input type="file" accept="image/*,.heic,.heif" multiple :disabled="replyBusy" @change="chooseReplyFiles">
            <strong>写真を選択</strong><span>または、ここへドラッグ＆ドロップ</span>
          </label>
          <div v-if="replyPreviewUrls.length" class="reply-preview-grid">
            <img v-for="(url,index) in replyPreviewUrls" :key="url" :src="url" :alt="`${index+1}枚目のプレビュー`">
          </div>
          <label class="reply-comment"><span>コメント（任意）</span><textarea v-model="replyComment" rows="3" :disabled="replyBusy" placeholder="コメントを入力"></textarea></label>
          <p v-if="replyError" class="reply-error" role="alert">{{ replyError }}</p>
          <p v-if="replyNeedsReauth && replyAuthUrl" class="reply-auth-help"><a :href="replyAuthUrl" target="_blank" rel="noopener noreferrer">新しいタブで再認証する</a><span>この画面を閉じなければ、選択中の写真とコメントを保持できます。</span></p>
          <p v-else-if="replyNeedsReauth" class="reply-auth-help">この画面を閉じず、元の共有リンクを別タブで開き直してください。選択中の写真とコメントを保持できます。</p>
          <div v-if="replyProgress !== null" class="reply-progress"><span :style="{width:`${replyProgress}%`}"></span><small>{{ replyProgress }}%</small></div>
          <div class="reply-submit-row"><span>{{ replyFiles.length }}枚選択中</span><button type="submit" class="primary-button" :disabled="replyBusy || !replyFiles.length">{{ replyBusy ? '送信中…' : replyNeedsReauth ? '認証後に再送' : '折り返しを送信' }}</button></div>
        </form>
        <details v-if="data.reply.canList" id="replies" class="reply-list-embedded reply-collapsible">
          <summary class="reply-section-heading"><div><h2>折り返し一覧</h2><p>アップロード日時ごとに表示します。</p></div><span aria-hidden="true">⌄</span></summary>
          <div class="reply-list-body">
            <div v-if="!data.reply.groups.length" class="empty-state">折り返しはまだありません。</div>
            <details v-for="group in data.reply.groups" :key="group.replyUuid" class="reply-group">
              <summary><time>{{ formatDateTime(group.postedAt) }}</time><strong>{{ group.count }}枚</strong><span aria-hidden="true">⌄</span></summary>
              <div class="reply-group-body">
                <div class="reply-image-grid">
                  <button v-for="(image,index) in group.images" :key="image.name" type="button" @click="openReplyLightbox(group,index)"><img :src="image.url" alt="折り返し画像" loading="lazy"></button>
                </div>
              </div>
            </details>
          </div>
        </details>
      </details>

      <details v-if="data.permissions.manageVisibility && !data.replyOnly" id="append" ref="appendDetails" class="append-upload-panel reply-collapsible">
        <summary class="reply-section-heading">
          <div><h2>写真を追加</h2><p>このアルバムにファイルを追加します。</p></div><span aria-hidden="true">⌄</span>
        </summary>
        <form class="append-upload-form" @submit.prevent="appendUploadFiles">
          <p class="append-upload-note">追加したファイルは公開されます。URL・保存期限は変わらず、相手への通知も送信しません。</p>
          <label class="reply-drop-zone" @dragover.prevent @drop.prevent="dropAppendFiles">
            <input type="file" accept="image/*,.heic,.heif,.cr2,.cr3,.nef,.nrw,.arw,.dng,.pdf,.zip" multiple :disabled="appendBusy" @change="chooseAppendFiles">
            <strong>追加するファイルを選択</strong><span>または、ここへドラッグ＆ドロップ</span>
          </label>
          <ul v-if="appendFiles.length" class="append-file-list"><li v-for="(file,index) in appendFiles" :key="`${index}-${file.name}`">{{ file.name }}</li></ul>
          <p v-if="appendResult" class="append-result" role="status">{{ appendResult }}</p>
          <ul v-if="appendRenames.length" class="append-file-list"><li v-for="(message,index) in appendRenames" :key="index">{{ message }}</li></ul>
          <ul v-if="appendErrors.length" class="append-errors" role="alert"><li v-for="(message,index) in appendErrors" :key="index">{{ message }}</li></ul>
          <div v-if="appendProgress !== null" class="reply-progress"><span :style="{width:`${appendProgress}%`}"></span><small>{{ appendProgressText }}（{{ appendProgress }}%）</small></div>
          <div class="reply-submit-row"><span>{{ appendFiles.length }}件選択中</span><button type="submit" class="primary-button" :disabled="appendBusy || !appendFiles.length">{{ appendBusy ? '追加中…' : 'ファイルを追加' }}</button></div>
        </form>
      </details>

      <section v-if="!data.replyOnly" ref="albumPanel" class="album-panel">
        <div class="album-toolbar">
          <div>
            <h2>写真・動画</h2>
            <p v-if="data.permissions.manageVisibility">公開中 {{ data.counts.public }}件 ／ 非公開 {{ data.counts.hidden }}件</p>
            <p v-if="data.files.some(file => file.flagged && !file.hidden)" class="flag-legend">🚩 赤枠の写真は加工バック希望です</p>
          </div>
          <div class="toolbar-actions">
            <template v-if="managing">
              <button v-for="item in (['all', 'public', 'hidden'] as const)" :key="item" type="button" :class="{active:filter===item}" @click="filter=item; clearSelection()">
                {{ item === 'all' ? 'すべて' : item === 'public' ? '公開中' : '非公開' }}
              </button>
              <button type="button" @click="stopManaging">管理終了</button>
            </template>
            <button v-else-if="data.permissions.manageVisibility" type="button" @click="startManaging">公開状態を管理</button>
          </div>
        </div>

        <div v-if="!displayedFiles.length" class="empty-state">表示できるファイルがありません。</div>
        <div v-else class="media-grid">
          <article v-for="file in displayedFiles" :key="file.id" class="media-card" :class="{'is-hidden':file.hidden,'is-flagged':file.flagged && file.kind === 'image'}">
            <button class="select-button" type="button" :aria-label="`${file.name}を選択`" :class="{selected:selected.includes(file.id)}" @click.stop="toggleSelection(file)">
              <span v-if="selected.includes(file.id)">✓</span>
            </button>
            <span v-if="file.hidden" class="hidden-badge">非公開</span>
            <span v-if="file.flagged && file.kind === 'image'" class="flag-badge" title="加工バック希望" aria-label="加工バック希望">🚩</span>
            <button v-if="file.kind !== 'file'" class="preview-button" type="button" @click="openLightbox(file)">
              <img v-if="file.kind === 'image'" :src="file.thumbnailUrl || file.url" :alt="file.name" loading="lazy">
              <video v-else :src="file.url" preload="metadata" muted playsinline></video>
              <span v-if="file.kind === 'video'" class="play-mark">▶</span>
            </button>
            <a v-else class="file-card" :href="file.url" download><span>📄</span><small>{{ file.name }}</small></a>
          </article>
        </div>
      </section>

      <div v-if="!data.replyOnly" class="bottom-spacer" :class="{'has-prepared-zip':!!preparedZip}"></div>
      <div v-if="!data.replyOnly" class="selection-bar">
        <button type="button" @click="selectAll">全選択</button>
        <button type="button" :disabled="!selected.length" @click="clearSelection">全解除</button>
        <strong>{{ selected.length }}件選択中</strong>
        <template v-if="managing">
          <button type="button" :disabled="busy || !selected.length" class="dark-button" @click="changeVisibility(true)">非公開</button>
          <button type="button" :disabled="busy || !selected.length" class="primary-button" @click="changeVisibility(false)">公開</button>
          <button type="button" :disabled="busy || !selectedImageIds.length" @click="changeFlags(true)">🚩を付ける</button>
          <button type="button" :disabled="busy || !selectedImageIds.length" @click="changeFlags(false)">🚩を外す</button>
        </template>
        <template v-else>
          <button type="button" :disabled="busy || !selected.length" class="primary-button" @click="zipDownload">ZIPでDL</button>
          <button v-if="isIos && data.download.mobileEnabled" type="button" :disabled="busy || !selectedFiles.some(file=>file.mobileDownload)" class="shortcut-button" @click="shortcutDownload">SCでDL</button>
        </template>
        <div v-if="preparedZip && !managing" class="prepared-zip" role="status">
          <span>ZIP準備完了（{{ preparedZip.count }}件・{{ Math.ceil(preparedZip.size / 1024 / 1024) }}MB）</span>
          <a :href="preparedZip.url" :download="preparedZip.name" class="prepared-zip-download" @click="clickPreparedZip">ZIPをダウンロード</a>
          <small>開始しない場合は、このボタンから同じZIPを再試行できます。</small>
        </div>
        <div v-if="progress !== null" class="progress-row"><span :style="{width:`${progress}%`}"></span><small>{{ progressText }}</small></div>
      </div>

      <div
        v-if="lightboxFile"
        class="lightbox"
        role="dialog"
        aria-modal="true"
        @click.self="closeLightbox"
        @touchstart.passive="startLightboxSwipe"
        @touchend.passive="finishLightboxSwipe"
        @touchcancel="lightboxTouchStart=null"
      >
        <button class="lightbox-close" type="button" aria-label="閉じる" @click="closeLightbox">×</button>
        <button class="lightbox-nav previous" type="button" aria-label="前へ" @click="moveLightbox(-1)" @dblclick.prevent>‹</button>
        <img v-if="lightboxFile.kind === 'image'" :src="lightboxFile.url" :alt="lightboxFile.name">
        <video v-else :src="lightboxFile.url" controls autoplay playsinline></video>
        <button class="lightbox-nav next" type="button" aria-label="次へ" @click="moveLightbox(1)" @dblclick.prevent>›</button>
        <div class="lightbox-footer">
          <span class="lightbox-filename">{{ lightboxFile.flagged ? `🚩　${lightboxFile.name}` : lightboxFile.name }}</span>
          <button
            v-if="data.permissions.manageVisibility"
            type="button"
            :disabled="busy"
            :class="lightboxFile.hidden ? 'make-public' : 'make-hidden'"
            @click="changeLightboxVisibility(lightboxFile)"
          >{{ lightboxFile.hidden ? '公開に戻す' : '非公開にする' }}</button>
          <button
            v-if="data.permissions.manageVisibility && lightboxFile.kind === 'image'"
            type="button"
            :disabled="busy"
            class="lightbox-flag-button"
            aria-keyshortcuts="E"
            @click="changeLightboxFlag(lightboxFile)"
          >{{ lightboxFile.flagged ? '🚩を外す' : '🚩を付ける' }}（E）</button>
        </div>
      </div>
      <div v-if="replyLightboxImage" class="lightbox" role="dialog" aria-modal="true" @click.self="closeReplyLightbox">
        <button class="lightbox-close" type="button" aria-label="閉じる" @click="closeReplyLightbox">×</button>
        <button class="lightbox-nav previous" type="button" aria-label="前へ" @click="moveReplyLightbox(-1)" @dblclick.prevent>‹</button>
        <img :src="replyLightboxImage.url" alt="折り返し画像">
        <button class="lightbox-nav next" type="button" aria-label="次へ" @click="moveReplyLightbox(1)" @dblclick.prevent>›</button>
      </div>
      <div v-if="toast" class="viewer-toast" aria-live="polite">{{ toast }}</div>
    </template>
  </main>
</template>
