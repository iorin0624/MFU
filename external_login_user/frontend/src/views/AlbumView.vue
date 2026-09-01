<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import InAppBrowserAlbumNotice from '@/components/InAppBrowserAlbumNotice.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { ApiError, portalApi } from '@/api/client';
import type { AlbumChild, AlbumDownloadJob, AlbumItem, MediaItem, Pagination, ProcessingState } from '@/types';
import { isInAppBrowser } from '@/utils/inAppBrowser';
import AlbumUploadDialog from '@/components/AlbumUploadDialog.vue';
import { usePortalStore } from '@/stores/portal';
import { CHILD_NAME_TEMPLATES, childTemplateMode } from '@/utils/albumUpload';

declare global {
  interface Window {
    MFUShortcutDownload?: {
      isIOSDevice: () => boolean;
      loadConfig: (force?: boolean) => Promise<{enabled: boolean}>;
      launch: (job: unknown) => Promise<unknown>;
    };
    MFUAdminPasskey?: { authorize: (action: string) => Promise<string> };
  }
}

type ChildGroup = { label: string; children: AlbumChild[] };

const route = useRoute();
const router = useRouter();
const portal = usePortalStore();
const uploadSelection = ref<{childId:string;childName:string;files:File[]}|null>(null);
const childTemplate = ref('');
const groupContainer = ref<HTMLElement|null>(null);
function setAllGroups(open:boolean) { groupContainer.value?.querySelectorAll('details.folder-group').forEach(item=>{(item as HTMLDetailsElement).open=open;}); }
function chooseChildTemplate() { childMode.value=childTemplateMode(childTemplate.value); }
const albumId = String(route.params.albumId);
const album = ref<AlbumItem | null>(null);
const canChooseChildType = computed(() => Boolean(album.value?.permissions.canChooseChildType));
const availableChildTemplates = computed(() => canChooseChildType.value ? CHILD_NAME_TEMPLATES : CHILD_NAME_TEMPLATES.filter(Boolean));
const children = ref<AlbumChild[]>([]);
const activeChild = ref<AlbumChild | null>(null);
const media = ref<MediaItem[]>([]);
const pagination = ref<Pagination | null>(null);
const loading = ref(true);
const mediaLoading = ref(false);
const busy = ref(false);
const error = ref('');
const blockedByInAppBrowser = isInAppBrowser();
const externalAlbumUrl = window.location.href;
const selected = ref<string[]>([]);
const sort = ref<'asc' | 'desc'>('asc');
const isMobile = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const loadSentinel = ref<HTMLElement | null>(null);
const dialog = ref<'create' | 'rename' | 'rename-album' | 'rename-media' | null>(null);
const target = ref<AlbumChild | null>(null);
const childName = ref('');
const childMode = ref<AlbumChild['mode']>('normal');
const albumName = ref('');
const mediaName = ref('');
const processingSelections = ref<Record<number, boolean>>({});
const processingBusy = ref(false);
const saving = ref(false);
const shortcutAvailable = ref(false);
const downloadProgress = ref<AlbumDownloadJob | null>(null);
const lightboxIndex = ref(-1);
let observer: IntersectionObserver | null = null;
let mobileQuery: MediaQueryList | null = null;
let downloadSocket: any = null;
let downloadFallbackTimer = 0;

const groups = computed<ChildGroup[]>(() => {
  const ordered = new Map<string, AlbumChild[]>();
  for (const child of children.value) {
    const label = childGroupLabel(child);
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
const childRouteId = computed(() => String(route.params.childId || ''));
const showChildList = computed(() => !isMobile.value || !childRouteId.value);
const showMediaList = computed(() => !isMobile.value || Boolean(childRouteId.value));
const processing = computed<ProcessingState | null>(() => activeChild.value?.processing || null);
const selectedMediaItem = computed(() => selected.value.length === 1
  ? media.value.find((item) => item.name === selected.value[0]) || null
  : null);

function childDisplayName(child: AlbumChild) {
  const name = child.processing?.completed
    ? String(child.name || '').replace(/^【加工回し】/, '【加工終了】')
    : String(child.name || '');
  return name.replace(/^【[^】]+】\s*/, '') || name;
}

function childGroupLabel(child: AlbumChild) {
  const name = child.processing?.completed
    ? String(child.name || '').replace(/^【加工回し】/, '【加工終了】')
    : String(child.name || '');
  return name.match(/^(【[^】]+】)/)?.[1] || 'その他';
}

function formatEventDate(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' }).format(date);
}

function formatDateTime(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date);
}

function formatRemaining(seconds?: number | null) {
  if (seconds == null) return '';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}分${String(rest).padStart(2, '0')}秒`;
}

function syncProcessingSelections(state?: ProcessingState | null) {
  processingSelections.value = Object.fromEntries(
    (state?.members || []).map((member) => [member.user_id, Boolean(member.requestFlag)]),
  );
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [albumResponse, childResponse] = await Promise.all([portalApi.album(albumId), portalApi.children(albumId)]);
    album.value = albumResponse.album;
    children.value = childResponse.children;
    const requested = childRouteId.value || String(route.query.child || '');
    const first = children.value.find((child) => child.id === requested) || (!isMobile.value ? children.value[0] : null) || null;
    if (first) await selectChild(first, false);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アルバムを取得できませんでした。';
  } finally {
    loading.value = false;
  }
}

async function selectChild(child: AlbumChild, updateUrl = true) {
  if (activeChild.value?.id === child.id && media.value.length) {
    if (updateUrl && isMobile.value && !childRouteId.value) {
      await router.push({ name: 'album-child', params: { albumId, childId: child.id } });
    }
    return;
  }
  activeChild.value = child;
  selected.value = [];
  media.value = [];
  pagination.value = null;
  lightboxIndex.value = -1;
  if (updateUrl) {
    if (isMobile.value) {
      await router.push({ name: 'album-child', params: { albumId, childId: child.id } });
    } else {
      await router.replace({ query: { ...route.query, child: child.id } });
    }
  }
  await loadMedia(1, false);
}

async function loadMedia(page = 1, append = false) {
  if (!activeChild.value || mediaLoading.value) return;
  mediaLoading.value = true;
  error.value = '';
  try {
    const childId = activeChild.value.id;
    const response = await portalApi.media(albumId, childId, page, sort.value);
    if (activeChild.value?.id !== childId) return;
    activeChild.value = response.child;
    const childIndex = children.value.findIndex((child) => child.id === response.child.id);
    if (childIndex >= 0) children.value[childIndex] = response.child;
    syncProcessingSelections(response.child.processing);
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
  childName.value = portal.session?.profile?.nickname || '';
  childTemplate.value = canChooseChildType.value ? '' : '【構図】';
  childMode.value = 'normal';
  dialog.value = 'create';
}

function openRename(child: AlbumChild) {
  target.value = child;
  childName.value = child.name;
  dialog.value = 'rename';
}

function openRenameAlbum() {
  if (!album.value) return;
  albumName.value = album.value.name;
  dialog.value = 'rename-album';
}

function openRenameMedia() {
  if (!selectedMediaItem.value) return;
  const name = selectedMediaItem.value.name;
  const dot = name.lastIndexOf('.');
  mediaName.value = dot > 0 ? name.slice(0, dot) : name;
  dialog.value = 'rename-media';
}

async function withPasskeyRetry<T>(action: string, task: (token?: string) => Promise<T>): Promise<T> {
  try {
    return await task();
  } catch (reason) {
    if (!(reason instanceof ApiError) || reason.status !== 428 || !window.MFUAdminPasskey) throw reason;
    const token = await window.MFUAdminPasskey.authorize(action);
    return task(token);
  }
}

async function saveDialog() {
  const name = dialog.value === 'rename-album'
    ? albumName.value.trim()
    : dialog.value === 'rename-media'
      ? mediaName.value.trim()
      : childName.value.trim();
  if (!name) return;
  saving.value = true;
  error.value = '';
  try {
    if (dialog.value === 'rename-album' && album.value) {
      const response = await portalApi.renameAlbum(albumId, name);
      album.value.name = response.album.name;
    } else if (dialog.value === 'rename-media' && activeChild.value && selectedMediaItem.value) {
      await portalApi.renameMedia(albumId, activeChild.value.id, selectedMediaItem.value.name, name);
      selected.value = [];
      await loadMedia(1, false);
    } else if (dialog.value === 'create') {
      const response = await portalApi.createChild(albumId, `${childTemplate.value}${name}`, childMode.value);
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
    await withPasskeyRetry(`album_child_delete:${albumId}:${child.id}`, (token) => portalApi.deleteChild(albumId, child.id, token));
    children.value = children.value.filter((item) => item.id !== child.id);
    if (activeChild.value?.id === child.id) {
      activeChild.value = null;
      media.value = [];
      if (isMobile.value) await router.replace({ name: 'album', params: { albumId } });
      else if (children.value[0]) await selectChild(children.value[0]);
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '削除できませんでした。';
  }
}

async function deleteAlbum() {
  if (!album.value || !window.confirm(`アルバム「${album.value.name}」を完全に削除しますか？\nこの操作は元に戻せません。`)) return;
  busy.value = true;
  try {
    await withPasskeyRetry(`album_delete:${albumId}`, (token) => portalApi.deleteAlbum(albumId, token));
    backToEvent();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アルバムを削除できませんでした。';
  } finally {
    busy.value = false;
  }
}

async function upload(files: FileList | null) {
  if (!files?.length || !activeChild.value) return;
  uploadSelection.value={childId:activeChild.value.id,childName:activeChild.value.name,files:activeChild.value.mode==='process'?[files[0]]:Array.from(files)};
  if (fileInput.value) fileInput.value.value = '';
}
async function uploaded(childId:string) { if(activeChild.value?.id===childId){try{await loadMedia(1,false);await refreshProcessing();}catch{error.value='アップロード後の一覧を更新できませんでした。再読み込みしてください。';}} }

async function refreshProcessing() {
  if (!activeChild.value || activeChild.value.mode !== 'process') return;
  const response = await portalApi.processing(albumId, activeChild.value.id);
  activeChild.value.processing = response.processing;
  const index = children.value.findIndex((child) => child.id === activeChild.value?.id);
  if (index >= 0) children.value[index] = { ...children.value[index], processing: response.processing };
  syncProcessingSelections(response.processing);
}

async function saveProcessingRequests() {
  if (!activeChild.value || !processing.value) return;
  processingBusy.value = true;
  error.value = '';
  try {
    const members = processing.value.members.map((member) => ({
      ext_user_id: member.user_id,
      request_flag: Boolean(processingSelections.value[member.user_id]),
      complete_flag: Boolean(member.completeFlag && processingSelections.value[member.user_id]),
    }));
    await portalApi.saveProcessingRequests(albumId, activeChild.value.id, members);
    await refreshProcessing();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加工依頼を保存できませんでした。';
  } finally {
    processingBusy.value = false;
  }
}

async function beginProcessing() {
  if (!activeChild.value) return;
  const popup = window.open('about:blank', '_blank');
  processingBusy.value = true;
  error.value = '';
  try {
    const response = await portalApi.beginProcessing(albumId, activeChild.value.id);
    activeChild.value.processing = response.processing;
    if (popup) popup.location.href = response.downloadUrl;
    else window.location.assign(response.downloadUrl);
  } catch (reason) {
    popup?.close();
    error.value = reason instanceof Error ? reason.message : '加工を開始できませんでした。';
  } finally {
    processingBusy.value = false;
  }
}

async function unlockProcessing(force = false) {
  if (!activeChild.value) return;
  if (!window.confirm(force ? '管理者として加工ロックを強制解除しますか？' : '加工ロックを解除しますか？')) return;
  processingBusy.value = true;
  try {
    const response = await portalApi.unlockProcessing(albumId, activeChild.value.id, force);
    activeChild.value.processing = response.processing;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加工ロックを解除できませんでした。';
  } finally {
    processingBusy.value = false;
  }
}

async function finishWithoutProcessing() {
  if (!activeChild.value || !processing.value?.currentExternalUserId) return;
  if (!window.confirm('加工不要として完了しますか？')) return;
  processingBusy.value = true;
  try {
    const status = processing.value.currentUserStatus;
    await portalApi.saveProcessingMember(
      albumId,
      activeChild.value.id,
      processing.value.currentExternalUserId,
      Boolean(status?.requestFlag),
      true,
    );
    await refreshProcessing();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '完了状態を保存できませんでした。';
  } finally {
    processingBusy.value = false;
  }
}

async function removeSelected() {
  if (!activeChild.value || !selected.value.length || !window.confirm(`${selected.value.length}件を削除しますか？`)) return;
  busy.value = true;
  try {
    const childId = activeChild.value.id;
    await withPasskeyRetry(`album_media_delete:${albumId}:${childId}`, (token) => portalApi.deleteMedia(albumId, childId, selected.value, token));
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
    downloadProgress.value = created.job;
    const completed = await waitForAlbumDownload(created.job.id);
    if (!completed.downloadUrl) throw new Error('ZIPのダウンロードURLを取得できませんでした。');
    window.location.assign(completed.downloadUrl);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'ZIPを生成できませんでした。';
  } finally { stopDownloadWatcher(); busy.value = false; }
}

function formatBytes(value?: number) {
  if (value == null) return '—';
  const units=['B','KB','MB','GB']; let size=value; let index=0;
  while(size>=1024&&index<units.length-1){size/=1024;index+=1;}
  return `${size>=10||index===0?Math.round(size):size.toFixed(1)} ${units[index]}`;
}
function stopDownloadWatcher() {
  window.clearInterval(downloadFallbackTimer); downloadFallbackTimer=0;
  downloadSocket?.disconnect?.(); downloadSocket=null;
}
function waitForAlbumDownload(jobId:string):Promise<AlbumDownloadJob> {
  stopDownloadWatcher();
  return new Promise((resolve,reject)=>{
    let finished=false;
    const accept=(job:AlbumDownloadJob)=>{
      if(finished)return; downloadProgress.value={...downloadProgress.value,...job,id:jobId};
      if(job.status==='done'){finished=true;stopDownloadWatcher();resolve(downloadProgress.value);}
      else if(job.status==='error'||job.status==='failed'){finished=true;stopDownloadWatcher();reject(new Error(job.error||'ZIP生成に失敗しました。'));}
    };
    const poll=()=>portalApi.albumDownloadStatus(albumId,jobId).then(response=>accept(response.job)).catch(()=>void 0);
    const startFallback=()=>{if(!downloadFallbackTimer)downloadFallbackTimer=window.setInterval(poll,3000);};
    const io=(window as any).io;
    if(typeof io==='function'){
      downloadSocket=io('/download-progress',{transports:['websocket','polling']});
      downloadSocket.on('connect',()=>{window.clearInterval(downloadFallbackTimer);downloadFallbackTimer=0;downloadSocket.emit('zip_progress_subscribe',{key:jobId},(reply:any)=>{if(reply?.progress)accept({...reply.progress,id:jobId});});});
      downloadSocket.on('zip_progress_update',(payload:any)=>{if(payload?.key===jobId&&payload.progress)accept({...payload.progress,id:jobId});});
      downloadSocket.on('disconnect',startFallback); downloadSocket.on('connect_error',startFallback);
    } else startFallback();
    void poll();
  });
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

function backToEvent() {
  const uuid = album.value?.event?.event_uuid;
  if (uuid) void router.push({ name: 'event', params: { uuid } });
  else void router.push({ name: 'events' });
}

function backToChildren() {
  void router.push({ name: 'album', params: { albumId } });
}

function updateMobileLayout(event?: MediaQueryListEvent) {
  const wasMobile = isMobile.value;
  isMobile.value = event?.matches ?? mobileQuery?.matches ?? false;
  if (wasMobile && !isMobile.value && !activeChild.value && children.value[0]) void selectChild(children.value[0]);
}

watch(sort, () => void loadMedia(1, false));
watch(childRouteId, (childId) => {
  if (!childId || !isMobile.value) return;
  const child = children.value.find((item) => item.id === childId);
  if (child && activeChild.value?.id !== child.id) void selectChild(child, false);
});

onMounted(async () => {
  if (blockedByInAppBrowser) {
    loading.value = false;
    return;
  }
  document.addEventListener('keydown', onKeydown);
  mobileQuery = window.matchMedia('(max-width: 560px)');
  updateMobileLayout();
  mobileQuery.addEventListener('change', updateMobileLayout);
  await load();
  const shortcut = window.MFUShortcutDownload;
  if (shortcut?.isIOSDevice()) {
    try { shortcutAvailable.value = Boolean((await shortcut.loadConfig()).enabled); } catch { shortcutAvailable.value = false; }
  }
});

onBeforeUnmount(() => {
  stopDownloadWatcher();
  observer?.disconnect();
  document.removeEventListener('keydown', onKeydown);
  mobileQuery?.removeEventListener('change', updateMobileLayout);
});
</script>

<template>
  <InAppBrowserAlbumNotice v-if="blockedByInAppBrowser" :target-url="externalAlbumUrl" />
  <LoadingBlock v-else-if="loading">アルバムを読み込んでいます</LoadingBlock>
  <div v-else-if="error && !album" class="alert error">{{ error }}</div>
  <template v-else-if="album">
    <header class="album-workspace-header">
      <button type="button" class="back-link" @click="backToEvent">← イベント詳細</button>
      <div class="album-title-block">
        <h1>{{ album.name }}</h1>
        <p v-if="album.event">
          <strong>{{ album.event.title }}</strong>
          <span v-if="album.event.starts_at">{{ formatEventDate(album.event.starts_at) }}</span>
          <span v-if="album.event.place_name">{{ album.event.place_name }}</span>
        </p>
      </div>
      <div class="album-header-actions">
        <span class="role-chip">👤 {{ roleLabel }}</span>
        <button v-if="album.permissions.canRename && album.accessMode !== 'event'" type="button" class="button secondary compact" @click="openRenameAlbum">アルバム名変更</button>
        <button v-if="album.permissions.canDeleteAlbum" type="button" class="button danger compact" :disabled="busy" @click="deleteAlbum">アルバム削除</button>
      </div>
    </header>

    <div v-if="error" class="alert error compact-alert">{{ error }}</div>

    <div class="album-workspace">
      <aside ref="groupContainer" v-show="showChildList" class="album-sidebar" aria-label="子アルバム">
        <div class="sidebar-title"><strong>子アルバム</strong><span>{{ children.length }}</span></div>
        <div class="album-group-actions"><button type="button" @click="setAllGroups(true)">すべて展開</button><button type="button" @click="setAllGroups(false)">すべて折りたたむ</button></div>
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

      <main v-show="showMediaList" class="album-media-panel">
        <template v-if="activeChild">
          <button v-if="isMobile" type="button" class="mobile-child-back" @click="backToChildren">← 子アルバム一覧</button>
          <div class="media-panel-header">
            <div><h2>{{ childDisplayName(activeChild) }}</h2><span>{{ total }}{{ activeChild.mediaUnit }}</span></div>
            <div class="media-panel-controls">
              <select v-model="sort" aria-label="並び順"><option value="asc">撮影順</option><option value="desc">撮影降順</option></select>
              <label v-if="activeChild.permissions.canUpload && (activeChild.mode !== 'process' || !media.length || processing?.currentUserHoldsLock)" class="button primary compact upload-button">
                ＋ 追加
                <input ref="fileInput" type="file" multiple :accept="activeChild.mode === 'movie' ? 'video/*' : 'image/*'" :disabled="busy" @change="upload(($event.target as HTMLInputElement).files)">
              </label>
              <button v-if="activeChild.permissions.canRenameChild" type="button" class="icon-action" title="名前変更" @click="openRename(activeChild)">✎</button>
              <button v-if="activeChild.permissions.canDeleteChild" type="button" class="icon-action danger-text" title="削除" @click="deleteChild(activeChild)">⋯</button>
            </div>
          </div>

          <section v-if="activeChild.mode === 'process' && processing" class="processing-workspace">
            <div :class="['processing-summary', { complete: processing.completed, locked: processing.lock }]">
              <div>
                <strong v-if="processing.completed">✅ 加工回しは完了しています</strong>
                <strong v-else-if="processing.lock">🔒 {{ processing.lock.username }} さんが加工中です</strong>
                <strong v-else>🛠️ 加工開始を待っています</strong>
                <span v-if="processing.lock?.remainingSeconds != null">残り {{ formatRemaining(processing.lock.remainingSeconds) }}</span>
              </div>
              <div v-if="processing.lock" class="processing-summary-actions">
                <button v-if="processing.canUnlock" type="button" class="button secondary compact" :disabled="processingBusy" @click="unlockProcessing(false)">ロック解除</button>
                <button v-if="processing.canForceUnlock" type="button" class="button danger compact" :disabled="processingBusy" @click="unlockProcessing(true)">強制解除</button>
              </div>
            </div>

            <div class="processing-columns">
              <section class="processing-card requester-card">
                <h3>📣 お願いする側</h3>
                <p>最初の画像を追加し、加工を依頼する参加者を選びます。加工用画像は常に最新1枚へ差し替わります。</p>
                <label v-if="activeChild.permissions.canUpload && !media.length" class="button primary processing-upload">
                  加工回し用画像を追加
                  <input type="file" accept="image/*" :disabled="busy" @change="upload(($event.target as HTMLInputElement).files)">
                </label>
                <div v-if="processing.members.length" class="processing-members">
                  <div class="processing-member processing-member-head"><span>参加者</span><span>加工希望</span><span>依頼</span><span>状態</span></div>
                  <label v-for="member in processing.members" :key="member.user_id" class="processing-member">
                    <span>{{ member.nickname || '名前未設定' }}</span>
                    <span>{{ member.process ? '🔵' : '—' }}</span>
                    <span><input v-model="processingSelections[member.user_id]" type="checkbox" :disabled="processingBusy || member.completeFlag"></span>
                    <span>{{ member.completeFlag ? '✅ 完了' : member.requestFlag ? '⏳ 依頼済み' : '未依頼' }}</span>
                  </label>
                  <button type="button" class="button warning processing-request-button" :disabled="processingBusy || !media.length" @click="saveProcessingRequests">
                    {{ processingBusy ? '保存中…' : '依頼先を保存して通知' }}
                  </button>
                </div>
                <p v-else class="muted">加工対象の参加者はいません。</p>
              </section>

              <section class="processing-card worker-card">
                <h3>🛠️ 引き受ける側</h3>
                <template v-if="processing.currentUserStatus?.requestFlag">
                  <div v-if="processing.currentUserStatus.completeFlag" class="processing-done">✅ あなたの加工は完了済みです。</div>
                  <template v-else>
                    <button v-if="!processing.lock" type="button" class="button primary wide" :disabled="processingBusy || !media.length" @click="beginProcessing">加工用画像をDLして開始</button>
                    <div v-else-if="processing.currentUserHoldsLock" class="worker-actions">
                      <p>加工が完了した画像をアップロードすると、ロック解除・完了記録・通知まで自動で行います。</p>
                      <label class="button primary wide processing-upload">
                        加工済み画像をアップロード
                        <input type="file" accept="image/*" :disabled="busy" @change="upload(($event.target as HTMLInputElement).files)">
                      </label>
                      <button type="button" class="button secondary wide" :disabled="processingBusy" @click="finishWithoutProcessing">加工不要として完了</button>
                    </div>
                    <div v-else class="alert warning compact-alert">現在は他の参加者が加工中です。ロック解除までお待ちください。</div>
                  </template>
                </template>
                <p v-else class="muted">あなたへの加工依頼はありません。</p>
              </section>
            </div>

            <details class="processing-history">
              <summary>加工履歴（{{ processing.history.length }}件）</summary>
              <ol v-if="processing.history.length">
                <li v-for="(entry, index) in [...processing.history].reverse()" :key="`${entry.timestamp || entry.datetime}-${index}`">
                  <strong>{{ entry.user || '不明' }}</strong><span>{{ entry.datetime || formatDateTime(entry.timestamp ? new Date(entry.timestamp * 1000).toISOString() : null) }}</span>
                </li>
              </ol>
              <p v-else class="muted">加工履歴はありません。</p>
            </details>
          </section>

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

    <div v-if="activeChild && media.length && showMediaList" class="album-selection-bar">
      <button type="button" @click="selectAll">全選択</button>
      <button type="button" @click="clearSelection">全解除</button>
      <strong>{{ selectedCountLabel }}</strong>
      <button type="button" class="zip-action" :disabled="!selected.length || busy" @click="downloadZip">{{ busy ? '処理中…' : 'ZIPでDL' }}</button>
      <button v-if="shortcutAvailable" type="button" class="shortcut-action" :disabled="!selected.length || busy" @click="downloadShortcut">SCでDL</button>
      <button v-if="activeChild.permissions.canRenameMedia && selected.length === 1 && activeChild.mode !== 'process'" type="button" :disabled="busy" @click="openRenameMedia">名前変更</button>
      <button v-if="activeChild.permissions.canDeleteMedia" type="button" class="delete-action" :disabled="!selected.length || busy" @click="removeSelected">削除</button>
    </div>
    <div v-if="busy && downloadProgress" class="album-download-progress" role="status" aria-live="polite">
      <div><strong>ZIPを生成しています</strong><span>{{ downloadProgress.processed_files || 0 }} / {{ downloadProgress.total_files || selected.length }}件</span></div>
      <progress :value="downloadProgress.percent || 0" max="100"></progress>
      <div><span>{{ downloadProgress.percent || 0 }}%</span><span>{{ formatBytes(downloadProgress.processed_bytes) }} / {{ formatBytes(downloadProgress.total_bytes) }}</span></div>
    </div>
  </template>

  <div v-if="dialog" class="modal-backdrop" @click.self="dialog = null">
    <form class="modal-card" @submit.prevent="saveDialog">
      <h2>{{ dialog === 'create' ? '子アルバムを作成' : dialog === 'rename-album' ? 'アルバム名を変更' : dialog === 'rename-media' ? 'ファイル名を変更' : '子アルバム名を変更' }}</h2>
      <label v-if="dialog === 'rename-album'">名前<input v-model="albumName" required maxlength="120" autofocus></label>
      <label v-else-if="dialog === 'rename-media'">名前（拡張子は変更されません）<input v-model="mediaName" required maxlength="200" autofocus></label>
      <label v-else>名前<input v-model="childName" required maxlength="120" autofocus></label>
      <label v-if="dialog === 'create'">名前テンプレート<select v-model="childTemplate" @change="chooseChildTemplate"><option v-for="template in availableChildTemplates" :key="template" :value="template">{{template||'指定なし'}}</option></select><small>作成する名前：{{childTemplate}}{{childName}}</small></label>
      <label v-if="dialog === 'create'">種類<select v-model="childMode" :disabled="!canChooseChildType"><option value="normal">写真</option><option value="movie">動画</option><option value="process">加工用</option></select><small v-if="!canChooseChildType">種類は名前テンプレートによって決まります。</small></label>
      <div class="modal-actions"><button type="button" class="button secondary" @click="dialog = null">キャンセル</button><button type="submit" class="button primary" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button></div>
    </form>
  </div>

  <AlbumUploadDialog v-if="uploadSelection" :album-id="albumId" :child-id="uploadSelection.childId" :child-name="uploadSelection.childName" :files="uploadSelection.files" @close="uploadSelection=null" @completed="uploaded" />
  <div v-if="visibleLightboxItem" class="album-lightbox" role="dialog" aria-modal="true" @click.self="closeViewer">
    <button type="button" class="lightbox-close" aria-label="閉じる" @click="closeViewer">×</button>
    <button type="button" class="lightbox-prev" aria-label="前へ" :disabled="lightboxIndex === 0" @click="moveViewer(-1)">‹</button>
    <img v-if="visibleLightboxItem.kind === 'image'" :src="visibleLightboxItem.viewUrl" alt="">
    <video v-else :src="visibleLightboxItem.viewUrl" controls autoplay playsinline></video>
    <button type="button" class="lightbox-next" aria-label="次へ" :disabled="lightboxIndex >= media.length - 1 && !pagination?.hasNext" @click="moveViewer(1)">›</button>
    <div class="lightbox-counter">{{ lightboxIndex + 1 }} / {{ total }}</div>
  </div>
</template>
