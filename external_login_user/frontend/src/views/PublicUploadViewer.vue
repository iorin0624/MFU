<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';

type PublicFile = {
  id: number;
  name: string;
  hidden: boolean;
  kind: 'image' | 'video' | 'file';
  url: string;
  thumbnailUrl: string | null;
  relativePath: string;
  mobileDownload: boolean;
  capturedAt: string | null;
};

type ReplyImage = { name: string; url: string };
type ReplyGroup = {
  id: number;
  replyUuid: string;
  postedAt: string;
  count: number;
  images: ReplyImage[];
  zipPrepareUrl: string;
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
      }): Promise<{ download_url: string }>;
      startDownload(url: string): void;
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
const csrfToken = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
const filenameCollator = new Intl.Collator('ja', { numeric: true, sensitivity: 'base' });
const data = ref<ViewerPayload | null>(null);
const loading = ref(true);
const error = ref('');
const selected = ref<number[]>([]);
const visibleLimit = ref(80);
const filter = ref<'all' | 'public' | 'hidden'>('all');
const managing = ref(false);
const busy = ref(false);
const toast = ref('');
const progress = ref<number | null>(null);
const progressText = ref('');
const lightboxIndex = ref(-1);
const replyFiles = ref<File[]>([]);
const replyComment = ref('');
const replyPreviewUrls = ref<string[]>([]);
const replyBusy = ref(false);
const replyProgress = ref<number | null>(null);
const replyError = ref('');
const replyLightboxImages = ref<ReplyImage[]>([]);
const replyLightboxIndex = ref(-1);
let lightboxTouchStart: { x: number; y: number; at: number } | null = null;

const filteredFiles = computed(() => {
  const files = data.value?.files || [];
  if (!managing.value || filter.value === 'all') return files;
  return files.filter((file) => filter.value === 'hidden' ? file.hidden : !file.hidden);
});
const displayedFiles = computed(() => filteredFiles.value.slice(0, visibleLimit.value));
const selectableFiles = computed(() => (data.value?.files || []).filter((file) => !file.hidden));
const selectedFiles = computed(() => (data.value?.files || []).filter((file) => selected.value.includes(file.id)));
const lightboxFiles = computed(() => displayedFiles.value.filter((file) => file.kind === 'image' || file.kind === 'video'));
const lightboxFile = computed(() => lightboxFiles.value[lightboxIndex.value] || null);
const replyLightboxImage = computed(() => replyLightboxImages.value[replyLightboxIndex.value] || null);
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

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const response = await fetch(`/view/${encodeURIComponent(config.uuid || '')}/api`, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    const payload = await response.json().catch(() => null) as ViewerPayload | null;
    if (!response.ok || !payload?.ok) throw new Error((payload as { message?: string } | null)?.message || '表示情報を取得できませんでした。');
    payload.files = sortFilesByCaptureTime(payload.files);
    data.value = payload;
    selected.value = selected.value.filter((id) => payload.files.some((file) => file.id === id && !file.hidden));
    await nextTick();
    const targetId = window.location.hash.replace(/^#/, '');
    if (targetId) document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '表示情報を取得できませんでした。';
  } finally {
    loading.value = false;
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
    progress.value = 100;
    progressText.value = '生成完了';
    window.MFUZipDownload.startDownload(result.download_url);
  } catch {
    showToast('ZIPの生成に失敗しました。');
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
}

function chooseReplyFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  setReplyFiles(Array.from(input.files || []));
  input.value = '';
}

function dropReplyFiles(event: DragEvent) {
  setReplyFiles(Array.from(event.dataTransfer?.files || []));
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
    await new Promise<void>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', data.value!.reply.uploadUrl);
      xhr.responseType = 'json';
      xhr.setRequestHeader('X-CSRF-Token', csrfToken);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) replyProgress.value = Math.round((event.loaded / event.total) * 100);
      };
      xhr.onload = () => {
        const payload = xhr.response as { message?: string } | null;
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(payload?.message || '折り返しを送信できませんでした。'));
      };
      xhr.onerror = () => reject(new Error('通信に失敗しました。'));
      xhr.send(form);
    });
    setReplyFiles([]);
    replyComment.value = '';
    await load();
    showToast('折り返しを送信しました。');
  } catch (reason) {
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

async function replyZipDownload(group: ReplyGroup) {
  if (busy.value) return;
  busy.value = true;
  try {
    const response = await fetch(group.zipPrepareUrl, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: '{}',
    });
    const payload = await response.json().catch(() => null) as { error?: string; progress_url?: string; download_url?: string } | null;
    if (!response.ok || !payload?.progress_url || !payload.download_url) throw new Error(payload?.error || 'ZIPを準備できませんでした。');
    for (;;) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      const progressResponse = await fetch(payload.progress_url, { credentials: 'same-origin', cache: 'no-store' });
      const status = await progressResponse.json().catch(() => null) as { status?: string; error?: string } | null;
      if (!progressResponse.ok) throw new Error(status?.error || 'ZIPの進捗を取得できませんでした。');
      if (status?.status === 'error') throw new Error(status.error || 'ZIPの生成に失敗しました。');
      if (status?.status === 'done') break;
    }
    window.location.assign(payload.download_url);
  } catch (reason) {
    showToast(reason instanceof Error ? reason.message : 'ZIPの生成に失敗しました。');
  } finally { busy.value = false; }
}

onMounted(() => {
  window.addEventListener('keydown', keydown);
  void load();
});
onUnmounted(() => {
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
        <details v-if="data.notice" class="notice-card">
          <summary><h2 class="notice-alert-title">⚠️お知らせ⚠️</h2><span aria-hidden="true">⌄</span></summary>
          <p>{{ data.notice }}</p>
        </details>
      </section>

      <section v-if="data.reply.enabled" id="reply" class="reply-upload-panel">
        <div class="reply-section-heading">
          <div><h2>折り返し</h2><p>加工済みの写真を選択して送信できます。</p></div>
        </div>
        <form class="reply-upload-form" @submit.prevent="submitReply">
          <label class="reply-drop-zone" @dragover.prevent @drop.prevent="dropReplyFiles">
            <input type="file" accept="image/*,.heic,.heif" multiple :disabled="replyBusy" @change="chooseReplyFiles">
            <strong>写真を選択</strong><span>または、ここへドラッグ＆ドロップ</span>
          </label>
          <div v-if="replyPreviewUrls.length" class="reply-preview-grid">
            <img v-for="(url,index) in replyPreviewUrls" :key="url" :src="url" :alt="`${index+1}枚目のプレビュー`">
          </div>
          <label class="reply-comment"><span>コメント（任意）</span><textarea v-model="replyComment" rows="3" :disabled="replyBusy" placeholder="コメントを入力"></textarea></label>
          <p v-if="replyError" class="reply-error">{{ replyError }}</p>
          <div v-if="replyProgress !== null" class="reply-progress"><span :style="{width:`${replyProgress}%`}"></span><small>{{ replyProgress }}%</small></div>
          <div class="reply-submit-row"><span>{{ replyFiles.length }}枚選択中</span><button type="submit" class="primary-button" :disabled="replyBusy || !replyFiles.length">{{ replyBusy ? '送信中…' : '折り返しを送信' }}</button></div>
        </form>
      </section>

      <section v-if="data.reply.canList" id="replies" class="reply-list-panel">
        <div class="reply-section-heading"><div><h2>折り返し一覧</h2><p>アップロード日時ごとに表示します。</p></div></div>
        <div v-if="!data.reply.groups.length" class="empty-state">折り返しはまだありません。</div>
        <details v-for="group in data.reply.groups" :key="group.replyUuid" class="reply-group">
          <summary><time>{{ formatDateTime(group.postedAt) }}</time><strong>{{ group.count }}枚</strong><span aria-hidden="true">⌄</span></summary>
          <div class="reply-group-body">
            <div class="reply-image-grid">
              <button v-for="(image,index) in group.images" :key="image.name" type="button" @click="openReplyLightbox(group,index)"><img :src="image.url" alt="折り返し画像" loading="lazy"></button>
            </div>
            <button type="button" class="outline-button reply-zip-button" :disabled="busy" @click="replyZipDownload(group)">この回をZIPでDL</button>
          </div>
        </details>
      </section>

      <section v-if="!data.replyOnly" class="album-panel">
        <div class="album-toolbar">
          <div>
            <h2>写真・動画</h2>
            <p v-if="data.permissions.manageVisibility">公開中 {{ data.counts.public }}件 ／ 非公開 {{ data.counts.hidden }}件</p>
          </div>
          <div class="toolbar-actions">
            <template v-if="managing">
              <button v-for="item in (['all', 'public', 'hidden'] as const)" :key="item" type="button" :class="{active:filter===item}" @click="filter=item; visibleLimit=80; clearSelection()">
                {{ item === 'all' ? 'すべて' : item === 'public' ? '公開中' : '非公開' }}
              </button>
              <button type="button" @click="stopManaging">管理終了</button>
            </template>
            <button v-else-if="data.permissions.manageVisibility" type="button" @click="startManaging">公開状態を管理</button>
          </div>
        </div>

        <div v-if="!displayedFiles.length" class="empty-state">表示できるファイルがありません。</div>
        <div v-else class="media-grid">
          <article v-for="file in displayedFiles" :key="file.id" class="media-card" :class="{'is-hidden':file.hidden}">
            <button class="select-button" type="button" :aria-label="`${file.name}を選択`" :class="{selected:selected.includes(file.id)}" @click.stop="toggleSelection(file)">
              <span v-if="selected.includes(file.id)">✓</span>
            </button>
            <span v-if="file.hidden" class="hidden-badge">非公開</span>
            <button v-if="file.kind !== 'file'" class="preview-button" type="button" @click="openLightbox(file)">
              <img v-if="file.kind === 'image'" :src="file.thumbnailUrl || file.url" :alt="file.name" loading="lazy">
              <video v-else :src="file.url" preload="metadata" muted playsinline></video>
              <span v-if="file.kind === 'video'" class="play-mark">▶</span>
            </button>
            <a v-else class="file-card" :href="file.url" download><span>📄</span><small>{{ file.name }}</small></a>
          </article>
        </div>
        <button v-if="visibleLimit < filteredFiles.length" class="load-more" type="button" @click="visibleLimit += 80">続きを表示</button>
      </section>

      <div v-if="!data.replyOnly" class="bottom-spacer"></div>
      <div v-if="!data.replyOnly" class="selection-bar">
        <button type="button" @click="selectAll">全選択</button>
        <button type="button" :disabled="!selected.length" @click="clearSelection">全解除</button>
        <strong>{{ selected.length }}件選択中</strong>
        <template v-if="managing">
          <button type="button" :disabled="busy || !selected.length" class="dark-button" @click="changeVisibility(true)">非公開</button>
          <button type="button" :disabled="busy || !selected.length" class="primary-button" @click="changeVisibility(false)">公開</button>
        </template>
        <template v-else>
          <button type="button" :disabled="busy || !selected.length" class="primary-button" @click="zipDownload">ZIPでDL</button>
          <button v-if="isIos && data.download.mobileEnabled" type="button" :disabled="busy || !selectedFiles.some(file=>file.mobileDownload)" class="shortcut-button" @click="shortcutDownload">SCでDL</button>
        </template>
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
          <span class="lightbox-filename">{{ lightboxFile.name }}</span>
          <button
            v-if="data.permissions.manageVisibility"
            type="button"
            :disabled="busy"
            :class="lightboxFile.hidden ? 'make-public' : 'make-hidden'"
            @click="changeLightboxVisibility(lightboxFile)"
          >{{ lightboxFile.hidden ? '公開に戻す' : '非公開にする' }}</button>
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
