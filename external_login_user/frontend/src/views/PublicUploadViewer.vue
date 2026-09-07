<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';

type PublicFile = {
  id: number;
  name: string;
  hidden: boolean;
  kind: 'image' | 'video' | 'file';
  url: string;
  thumbnailUrl: string | null;
  relativePath: string;
  mobileDownload: boolean;
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
  reply: { enabled: boolean; url: string };
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
const config = JSON.parse(configElement?.textContent || '{}') as { uuid?: string };
const csrfToken = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
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
const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

function formatDate(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');
  return match ? `${match[1]}年${match[2]}月${match[3]}日` : value;
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const response = await fetch(`/view/${encodeURIComponent(config.uuid || '')}/api`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const payload = await response.json().catch(() => null) as ViewerPayload | null;
    if (!response.ok || !payload?.ok) throw new Error((payload as { message?: string } | null)?.message || '表示情報を取得できませんでした。');
    data.value = payload;
    selected.value = selected.value.filter((id) => payload.files.some((file) => file.id === id && !file.hidden));
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
  if (lightboxIndex.value < 0) return;
  if (event.key === 'Escape') closeLightbox();
  if (event.key === 'ArrowLeft') moveLightbox(-1);
  if (event.key === 'ArrowRight') moveLightbox(1);
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
  if (paths.length === selectableFiles.value.length) {
    window.location.href = data.value.download.zipUrl;
    return;
  }
  if (!window.MFUZipDownload) return showToast('ZIPダウンロードを開始できませんでした。');
  busy.value = true;
  progress.value = 0;
  progressText.value = 'ZIPを準備しています';
  try {
    const result = await window.MFUZipDownload.prepare({
      paths,
      csrfToken,
      key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
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

async function changeLightboxVisibility(file: PublicFile) {
  if (busy.value || !data.value?.permissions.manageVisibility) return;
  const hidden = !file.hidden;
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

onMounted(() => {
  window.addEventListener('keydown', keydown);
  void load();
});
onUnmounted(() => window.removeEventListener('keydown', keydown));
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

      <section v-if="data.notice || data.reply.enabled" class="notice-grid">
        <details v-if="data.notice" class="notice-card">
          <summary><h2>お知らせ</h2><span aria-hidden="true">⌄</span></summary>
          <p>{{ data.notice }}</p>
        </details>
        <article v-if="data.reply.enabled" class="reply-card">
          <div><h2>折り返し</h2><p>加工済みの写真はこちらから送信できます。</p></div>
          <a class="primary-button" :href="data.reply.url">折り返し</a>
        </article>
      </section>

      <section class="album-panel">
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

      <div class="bottom-spacer"></div>
      <div class="selection-bar">
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

      <div v-if="lightboxFile" class="lightbox" role="dialog" aria-modal="true" @click.self="closeLightbox">
        <button class="lightbox-close" type="button" aria-label="閉じる" @click="closeLightbox">×</button>
        <button class="lightbox-nav previous" type="button" aria-label="前へ" @click="moveLightbox(-1)">‹</button>
        <img v-if="lightboxFile.kind === 'image'" :src="lightboxFile.url" :alt="lightboxFile.name">
        <video v-else :src="lightboxFile.url" controls autoplay playsinline></video>
        <button class="lightbox-nav next" type="button" aria-label="次へ" @click="moveLightbox(1)">›</button>
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
      <div v-if="toast" class="viewer-toast" aria-live="polite">{{ toast }}</div>
    </template>
  </main>
</template>
