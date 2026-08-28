<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { AlbumChild, AlbumItem, MediaItem, Pagination } from '@/types';

declare global {
  interface Window {
    MFUShortcutDownload?: {
      isIOSDevice: () => boolean;
      loadConfig: (force?: boolean) => Promise<{enabled: boolean}>;
      launch: (job: unknown) => Promise<unknown>;
    };
  }
}

type ChildGroup = { label: string; children: AlbumChild[] };

const route = useRoute();
const router = useRouter();
const albumId = String(route.params.albumId);
const album = ref<AlbumItem | null>(null);
const children = ref<AlbumChild[]>([]);
const activeChild = ref<AlbumChild | null>(null);
const media = ref<MediaItem[]>([]);
const pagination = ref<Pagination | null>(null);
const loading = ref(true);
const mediaLoading = ref(false);
const busy = ref(false);
const error = ref('');
const selected = ref<string[]>([]);
const search = ref('');
const sort = ref<'asc' | 'desc'>('asc');
const fileInput = ref<HTMLInputElement | null>(null);
const loadSentinel = ref<HTMLElement | null>(null);
const dialog = ref<'create' | 'rename' | null>(null);
const target = ref<AlbumChild | null>(null);
const childName = ref('');
const childMode = ref<AlbumChild['mode']>('normal');
const saving = ref(false);
const shortcutAvailable = ref(false);
const lightboxIndex = ref(-1);
let observer: IntersectionObserver | null = null;
let searchTimer = 0;

const groups = computed<ChildGroup[]>(() => {
  const ordered = new Map<string, AlbumChild[]>();
  for (const child of children.value) {
    const match = String(child.name || '').match(/^(【[^】]+】)/);
    const label = match?.[1] || 'その他';
    if (!ordered.has(label)) ordered.set(label, []);
    ordered.get(label)?.push(child);
  }
  return Array.from(ordered, ([label, values]) => ({ label, children: values }));
});

const total = computed(() => pagination.value?.total ?? activeChild.value?.mediaCount ?? 0);
const selectedCountLabel = computed(() => `${selected.value.length}${activeChild.value?.mediaUnit || '枚'}選択中`);
const roleLabel = computed(() => ({
  admin: '管理者', event_acl: 'イベント管理者', owner: '所有者', event_member: '参加者', token_viewer: '閲覧者',
}[album.value?.permissions.role || ''] || '閲覧者'));
const visibleLightboxItem = computed(() => lightboxIndex.value >= 0 ? media.value[lightboxIndex.value] : null);

function childDisplayName(child: AlbumChild) {
  return String(child.name || '').replace(/^【[^】]+】\s*/, '') || child.name;
}

function formatEventDate(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' }).format(date);
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [albumResponse, childResponse] = await Promise.all([portalApi.album(albumId), portalApi.children(albumId)]);
    album.value = albumResponse.album;
    children.value = childResponse.children;
    const requested = String(route.query.child || '');
    const first = children.value.find((child) => child.id === requested) || children.value[0] || null;
    if (first) await selectChild(first, false);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アルバムを取得できませんでした。';
  } finally {
    loading.value = false;
  }
}

async function selectChild(child: AlbumChild, updateUrl = true) {
  if (activeChild.value?.id === child.id && media.value.length) return;
  activeChild.value = child;
  selected.value = [];
  media.value = [];
  pagination.value = null;
  lightboxIndex.value = -1;
  if (updateUrl) await router.replace({ query: { ...route.query, child: child.id } });
  await loadMedia(1, false);
}

async function loadMedia(page = 1, append = false) {
  if (!activeChild.value || mediaLoading.value) return;
  mediaLoading.value = true;
  error.value = '';
  try {
    const childId = activeChild.value.id;
    const response = await portalApi.media(albumId, childId, page, sort.value, search.value);
    if (activeChild.value?.id !== childId) return;
    activeChild.value = response.child;
    media.value = append ? [...media.value, ...response.media] : response.media;
    pagination.value = response.pagination;
    await nextTick();
    observeSentinel();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '写真・動画を取得できませんでした。';
  } finally {
    mediaLoading.value = false;
  }
}

async function loadMore() {
  if (pagination.value?.hasNext) await loadMedia(pagination.value.page + 1, true);
}

function observeSentinel() {
  observer?.disconnect();
  if (!loadSentinel.value) return;
  observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) void loadMore();
  }, { rootMargin: '500px 0px' });
  observer.observe(loadSentinel.value);
}

function toggle(name: string) {
  selected.value = selected.value.includes(name) ? selected.value.filter((value) => value !== name) : [...selected.value, name];
}

async function selectAll() {
  if (!activeChild.value) return;
  busy.value = true;
  try {
    while (pagination.value?.hasNext) await loadMore();
    selected.value = media.value.map((item) => item.name);
  } finally {
    busy.value = false;
  }
}

function clearSelection() { selected.value = []; }

function openCreate() {
  target.value = null;
  childName.value = '';
  childMode.value = 'normal';
  dialog.value = 'create';
}

function openRename(child: AlbumChild) {
  target.value = child;
  childName.value = child.name;
  dialog.value = 'rename';
}

async function saveDialog() {
  const name = childName.value.trim();
  if (!name) return;
  saving.value = true;
  error.value = '';
  try {
    if (dialog.value === 'create') {
      const response = await portalApi.createChild(albumId, name, childMode.value);
      children.value.push(response.child);
      await selectChild(response.child);
    } else if (target.value) {
      const response = await portalApi.renameChild(albumId, target.value.id, name);
      const index = children.value.findIndex((child) => child.id === target.value?.id);
      if (index >= 0) children.value[index] = response.child;
      if (activeChild.value?.id === response.child.id) activeChild.value = response.child;
    }
    dialog.value = null;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '保存できませんでした。';
  } finally {
    saving.value = false;
  }
}

async function deleteChild(child: AlbumChild) {
  if (!window.confirm(`「${child.name}」を削除しますか？`)) return;
  try {
    await portalApi.deleteChild(albumId, child.id);
    children.value = children.value.filter((item) => item.id !== child.id);
    if (activeChild.value?.id === child.id) {
      activeChild.value = null;
      media.value = [];
      if (children.value[0]) await selectChild(children.value[0]);
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '削除できませんでした。';
  }
}

async function upload(files: FileList | null) {
  if (!files?.length || !activeChild.value) return;
  busy.value = true;
  try {
    await portalApi.uploadMedia(albumId, activeChild.value.id, Array.from(files));
    await loadMedia(1, false);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アップロードできませんでした。';
  } finally {
    busy.value = false;
    if (fileInput.value) fileInput.value.value = '';
  }
}

async function removeSelected() {
  if (!activeChild.value || !selected.value.length || !window.confirm(`${selected.value.length}件を削除しますか？`)) return;
  busy.value = true;
  try {
    await portalApi.deleteMedia(albumId, activeChild.value.id, selected.value);
    selected.value = [];
    await loadMedia(1, false);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '削除できませんでした。';
  } finally { busy.value = false; }
}

async function downloadZip() {
  if (!activeChild.value || !selected.value.length) return;
  busy.value = true;
  try {
    const created = await portalApi.createAlbumDownload(albumId, activeChild.value.id, selected.value);
    for (let count = 0; count < 600; count += 1) {
      const response = await portalApi.albumDownloadStatus(albumId, created.job.id);
      if (response.job.status === 'done' && response.job.downloadUrl) {
        window.location.assign(response.job.downloadUrl);
        return;
      }
      if (response.job.status === 'error' || response.job.status === 'failed') throw new Error(response.job.error || 'ZIP生成に失敗しました。');
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error('ZIP生成がタイムアウトしました。');
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'ZIPを生成できませんでした。';
  } finally { busy.value = false; }
}

async function downloadShortcut() {
  if (!activeChild.value || !selected.value.length || !window.MFUShortcutDownload) return;
  busy.value = true;
  try {
    const job = await portalApi.createShortcutDownload(albumId, activeChild.value.id, selected.value);
    await window.MFUShortcutDownload.launch(job);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'ショートカットを起動できませんでした。';
  } finally { window.setTimeout(() => { busy.value = false; }, 800); }
}

function openViewer(index: number) { lightboxIndex.value = index; }
function closeViewer() { lightboxIndex.value = -1; }
function moveViewer(delta: number) {
  const next = lightboxIndex.value + delta;
  if (next >= 0 && next < media.value.length) lightboxIndex.value = next;
  else if (delta > 0 && pagination.value?.hasNext) void loadMore().then(() => { if (next < media.value.length) lightboxIndex.value = next; });
}

function onKeydown(event: KeyboardEvent) {
  if (lightboxIndex.value < 0) return;
  if (event.key === 'Escape') closeViewer();
  if (event.key === 'ArrowLeft') moveViewer(-1);
  if (event.key === 'ArrowRight') moveViewer(1);
}

watch([sort, search], () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => void loadMedia(1, false), 260);
});

onMounted(async () => {
  document.addEventListener('keydown', onKeydown);
  await load();
  const shortcut = window.MFUShortcutDownload;
  if (shortcut?.isIOSDevice()) {
    try { shortcutAvailable.value = Boolean((await shortcut.loadConfig()).enabled); } catch { shortcutAvailable.value = false; }
  }
});

onBeforeUnmount(() => {
  observer?.disconnect();
  document.removeEventListener('keydown', onKeydown);
  window.clearTimeout(searchTimer);
});
</script>

<template>
  <LoadingBlock v-if="loading">アルバムを読み込んでいます</LoadingBlock>
  <div v-else-if="error && !album" class="alert error">{{ error }}</div>
  <template v-else-if="album">
    <header class="album-workspace-header">
      <button type="button" class="back-link" @click="router.back()">← アルバム一覧</button>
      <div class="album-title-block">
        <h1>{{ album.name }}</h1>
        <p v-if="album.event">
          <strong>{{ album.event.title }}</strong>
          <span v-if="album.event.starts_at">{{ formatEventDate(album.event.starts_at) }}</span>
          <span v-if="album.event.place_name">{{ album.event.place_name }}</span>
        </p>
      </div>
      <span class="role-chip">👤 {{ roleLabel }}</span>
    </header>

    <div v-if="error" class="alert error compact-alert">{{ error }}</div>

    <div class="album-workspace">
      <aside class="album-sidebar" aria-label="子アルバム">
        <div class="sidebar-title"><strong>子アルバム</strong><span>{{ children.length }}</span></div>
        <EmptyState v-if="!children.length" icon="📂" title="まだありません" text="" />
        <details v-for="group in groups" v-else :key="group.label" class="folder-group" open>
          <summary>📁 {{ group.label }} <small>{{ group.children.length }}</small></summary>
          <div class="folder-children">
            <button
              v-for="child in group.children"
              :key="child.id"
              type="button"
              :class="['folder-row', { active: activeChild?.id === child.id }]"
              @click="selectChild(child)"
            >
              <span>{{ child.mode === 'movie' ? '🎬' : child.mode === 'process' ? '🛠️' : '📁' }}</span>
              <span class="folder-name">{{ childDisplayName(child) }}</span>
              <small>{{ child.mediaCount }}{{ child.mediaUnit }}</small>
            </button>
          </div>
        </details>
        <button v-if="album.permissions.canCreateChild" type="button" class="sidebar-add" @click="openCreate">＋ 追加</button>
      </aside>

      <main class="album-media-panel">
        <template v-if="activeChild">
          <div class="media-panel-header">
            <div><h2>{{ childDisplayName(activeChild) }}</h2><span>{{ total }}{{ activeChild.mediaUnit }}</span></div>
            <div class="media-panel-controls">
              <label class="media-search">🔍<input v-model="search" type="search" placeholder="写真を検索"></label>
              <select v-model="sort" aria-label="並び順"><option value="asc">名前順</option><option value="desc">名前順（降順）</option></select>
              <label v-if="activeChild.permissions.canUpload" class="button primary compact upload-button">
                ＋ 追加
                <input ref="fileInput" type="file" multiple :accept="activeChild.mode === 'movie' ? 'video/*' : 'image/*'" :disabled="busy" @change="upload(($event.target as HTMLInputElement).files)">
              </label>
              <button v-if="activeChild.permissions.canRenameChild" type="button" class="icon-action" title="名前変更" @click="openRename(activeChild)">✎</button>
              <button v-if="activeChild.permissions.canDeleteChild" type="button" class="icon-action danger-text" title="削除" @click="deleteChild(activeChild)">⋯</button>
            </div>
          </div>

          <EmptyState v-if="!media.length && !mediaLoading" :icon="activeChild.mode === 'movie' ? '🎬' : '🖼️'" title="まだファイルがありません" text="" />
          <div v-else class="workspace-media-grid" :aria-busy="mediaLoading || busy">
            <article v-for="(item, index) in media" :key="item.name" :class="['workspace-media-card', { selected: selected.includes(item.name) }]">
              <button type="button" class="select-media" :aria-label="`${item.name}を選択`" :aria-pressed="selected.includes(item.name)" @click="toggle(item.name)"><span>{{ selected.includes(item.name) ? '✓' : '' }}</span></button>
              <button type="button" class="media-preview" @click="openViewer(index)">
                <img v-if="item.thumbnailUrl || item.posterUrl || item.kind === 'image'" :src="item.thumbnailUrl || item.posterUrl || item.viewUrl" alt="" loading="lazy">
                <video v-else-if="item.kind === 'video'" :src="item.viewUrl" preload="metadata" muted></video>
                <span v-else class="media-placeholder">🖼️</span>
                <span v-if="item.kind === 'video'" class="video-badge">▶</span>
                <span v-if="item.converting" class="video-badge converting">変換中</span>
              </button>
            </article>
          </div>
          <div ref="loadSentinel" class="load-sentinel" aria-hidden="true"></div>
          <p v-if="mediaLoading" class="inline-loading">続きを読み込んでいます…</p>
        </template>
        <EmptyState v-else icon="📂" title="子アルバムを選んでください" text="" />
      </main>
    </div>

    <div v-if="activeChild && media.length" class="album-selection-bar">
      <button type="button" @click="selectAll">全選択</button>
      <button type="button" @click="clearSelection">全解除</button>
      <strong>{{ selectedCountLabel }}</strong>
      <button type="button" class="zip-action" :disabled="!selected.length || busy" @click="downloadZip">{{ busy ? '処理中…' : 'ZIPでDL' }}</button>
      <button v-if="shortcutAvailable" type="button" class="shortcut-action" :disabled="!selected.length || busy" @click="downloadShortcut">SCでDL</button>
      <button v-if="activeChild.permissions.canDeleteMedia" type="button" :disabled="!selected.length || busy" @click="removeSelected">その他</button>
    </div>
  </template>

  <div v-if="dialog" class="modal-backdrop" @click.self="dialog = null">
    <form class="modal-card" @submit.prevent="saveDialog">
      <h2>{{ dialog === 'create' ? '子アルバムを作成' : '子アルバム名を変更' }}</h2>
      <label>名前<input v-model="childName" required maxlength="120" autofocus></label>
      <label v-if="dialog === 'create'">種類<select v-model="childMode"><option value="normal">写真</option><option value="movie">動画</option><option value="process">加工用</option></select></label>
      <div class="modal-actions"><button type="button" class="button secondary" @click="dialog = null">キャンセル</button><button type="submit" class="button primary" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button></div>
    </form>
  </div>

  <div v-if="visibleLightboxItem" class="album-lightbox" role="dialog" aria-modal="true" @click.self="closeViewer">
    <button type="button" class="lightbox-close" aria-label="閉じる" @click="closeViewer">×</button>
    <button type="button" class="lightbox-prev" aria-label="前へ" :disabled="lightboxIndex === 0" @click="moveViewer(-1)">‹</button>
    <img v-if="visibleLightboxItem.kind === 'image'" :src="visibleLightboxItem.viewUrl" alt="">
    <video v-else :src="visibleLightboxItem.viewUrl" controls autoplay playsinline></video>
    <button type="button" class="lightbox-next" aria-label="次へ" :disabled="lightboxIndex >= media.length - 1 && !pagination?.hasNext" @click="moveViewer(1)">›</button>
    <div class="lightbox-counter">{{ lightboxIndex + 1 }} / {{ total }}</div>
  </div>
</template>
