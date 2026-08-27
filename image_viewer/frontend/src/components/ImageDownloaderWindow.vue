<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { runtimeConfig } from '@/config';
import { requestJson } from '@/api/client';
import { useDesktopStore } from '@/stores/desktop';
import { useDownloaderStore } from '@/stores/downloader';
import { useExplorerStore } from '@/stores/explorer';
import type { DownloadImageItem } from '@/types';
import { delay, extractSupportedMediaUrls } from '@/utils/mediaUrls';

const desktop = useDesktopStore();
const downloader = useDownloaderStore();
const explorer = useExplorerStore();
const url = ref(downloader.imageUrl);
const folder = ref('instagram');
const startNumber = ref(1);
const digits = ref(3);
const selected = ref(new Set<number>());
const status = ref('');
const fetching = ref(false);
const saving = ref(false);
const activeJobId = ref('');
let clipboardTimer = 0;
let numberTimer = 0;

const images = computed(() => downloader.images);
const readyImages = computed(() => images.value.filter((item) => item.previewReady && item.previewUrl));

function restoreSettings() {
  try {
    const settings = JSON.parse(localStorage.getItem('mfu.imageViewer.instagramSettings') || '{}');
    folder.value = settings.folder || 'instagram';
    digits.value = Math.min(6, Math.max(1, Number(settings.digits) || 3));
  } catch { /* optional */ }
}

function saveSettings() {
  localStorage.setItem('mfu.imageViewer.instagramSettings', JSON.stringify({
    folder: folder.value || 'instagram', digits: digits.value || 3,
  }));
}

function applyClipboardText(text: string, announce = false) {
  const next = extractSupportedMediaUrls(text)[0];
  if (!next) {
    if (announce) status.value = 'クリップボードにInstagram / Threads / X のURLがありません。';
    return false;
  }
  url.value = next;
  downloader.imageUrl = next;
  if (announce) status.value = `${next} をクリップボードから入力しました。`;
  return true;
}

async function readClipboard(announce = true) {
  try {
    applyClipboardText(await navigator.clipboard.readText(), announce);
  } catch {
    if (announce) status.value = 'クリップボードを読み取れません。URLを貼り付けてください。';
  }
}

function onPaste(event: ClipboardEvent) {
  const text = event.clipboardData?.getData('text/plain') || '';
  if (!extractSupportedMediaUrls(text).length) return;
  event.preventDefault();
  applyClipboardText(text, true);
}

async function updateNextNumber() {
  const target = new URL(runtimeConfig.instagramNextNumberUrl, window.location.origin);
  target.searchParams.set('folder', folder.value || '');
  const data = await requestJson<{ok: boolean; nextNumber?: number}>(`${target.pathname}${target.search}`);
  startNumber.value = Number(data.nextNumber || 1);
}

async function pollJob(jobId: string) {
  const endpoint = runtimeConfig.instagramJobUrl.replace('__JOB_ID__', encodeURIComponent(jobId));
  const started = Date.now();
  let count = 0;
  while (Date.now() - started < 15 * 60 * 1000) {
    await delay(count < 3 ? 700 : 1200);
    count += 1;
    const job = await requestJson<{
      ok: boolean; status?: string; shortcode?: string; images?: DownloadImageItem[];
      total?: number; processed?: number; downloaded?: number; failed?: number;
      loginRequired?: boolean; error?: string;
    }>(endpoint);
    if (job.status === 'login_required' || job.loginRequired) {
      desktop.openUtility('instagram-auth');
      throw new Error(job.error || 'Instagramの再ログインが必要です。VNCでOTPを入力してください。');
    }
    if (Array.isArray(job.images) && job.images.length) {
      downloader.setImages({
        url: url.value, identifier: job.shortcode || '', jobId, images: job.images,
      });
      selected.value = new Set(job.images.filter((item) => item.previewReady).map((item) => item.index));
    }
    const total = Number(job.total || 0);
    const processed = Number(job.processed || 0);
    status.value = total
      ? `画像をダウンロード中... ${processed}/${total}（成功 ${job.downloaded || 0}・失敗 ${job.failed || 0}） ${Math.floor((Date.now() - started) / 1000)}秒`
      : `投稿画像を取得中... ${Math.floor((Date.now() - started) / 1000)}秒`;
    if (job.status === 'done' || job.status === 'cancelled') return job;
  }
  throw new Error('取得がタイムアウトしました。もう一度お試しください。');
}

async function fetchImages() {
  fetching.value = true;
  activeJobId.value = '';
  downloader.setImages({ url: url.value, images: [] });
  selected.value = new Set();
  status.value = '取得中...';
  try {
    const data = await requestJson<{ok: boolean; jobId?: string; shortcode?: string; images?: DownloadImageItem[]}>(
      runtimeConfig.instagramFetchUrl,
      { method: 'POST', body: JSON.stringify({ url: url.value.trim() }) },
    );
    activeJobId.value = data.jobId || '';
    const result = data.jobId ? await pollJob(data.jobId) : data;
    if ('status' in result && result.status === 'cancelled') {
      status.value = '取得をキャンセルしました。';
      return;
    }
    const nextImages = result.images || [];
    downloader.setImages({
      url: url.value, identifier: result.shortcode || data.shortcode || '',
      jobId: data.jobId || '', images: nextImages,
    });
    selected.value = new Set(nextImages.map((item) => item.index));
    saveSettings();
    await updateNextNumber();
    status.value = `${nextImages.length}枚取得しました。`;
  } catch (error) {
    downloader.setImages({ url: url.value, images: [] });
    selected.value = new Set();
    status.value = error instanceof Error ? error.message : '取得に失敗しました。';
  } finally {
    fetching.value = false;
    activeJobId.value = '';
  }
}

async function cancelFetch() {
  if (!activeJobId.value) return;
  status.value = 'キャンセル中...';
  try {
    await requestJson(runtimeConfig.instagramCancelJobUrl.replace('__JOB_ID__', encodeURIComponent(activeJobId.value)), {
      method: 'POST', body: JSON.stringify({}),
    });
    status.value = '取得をキャンセルしました。';
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'キャンセルできませんでした。';
  }
}

function toggle(index: number) {
  const next = new Set(selected.value);
  if (next.has(index)) next.delete(index); else next.add(index);
  selected.value = next;
}
function selectAll() { selected.value = new Set(images.value.map((item) => item.index)); }
function clearAll() { selected.value = new Set(); }
function invert() {
  selected.value = new Set(images.value.filter((item) => !selected.value.has(item.index)).map((item) => item.index));
}

async function refreshExplorers() {
  const entries = desktop.windows.filter((win) => win.explorer);
  await Promise.all(entries.map((win) => explorer.refreshFolder(win.explorer!.folder)));
}

async function saveImages() {
  if (!selected.value.size) { status.value = '保存する画像を選択してください。'; return; }
  saving.value = true;
  status.value = '保存中...';
  try {
    const data = await requestJson<{
      ok: boolean; saved?: unknown[]; duplicates?: Array<{existing?: {path?: string}}>; errors?: unknown[];
    }>(runtimeConfig.instagramSaveUrl, {
      method: 'POST',
      body: JSON.stringify({
        shortcode: downloader.imageIdentifier, jobId: downloader.imageJobId,
        images: images.value, selected: [...selected.value], folder: folder.value,
        startNumber: startNumber.value, digits: digits.value,
      }),
    });
    const duplicates = data.duplicates || [];
    const paths = duplicates.slice(0, 3).map((row) => row.existing?.path).filter(Boolean);
    status.value = `${data.saved?.length || 0}枚保存しました。${duplicates.length ? ` 重複${duplicates.length}件は保存しませんでした。${paths.length ? ` 既存: ${paths.join(', ')}` : ''}` : ''}`;
    saveSettings();
    await refreshExplorers();
    await updateNextNumber();
  } catch (error) {
    status.value = error instanceof Error ? error.message : '保存に失敗しました。';
  } finally { saving.value = false; }
}

watch(url, (value) => { downloader.imageUrl = value; });
watch(images, (values) => {
  selected.value = new Set(values.map((item) => item.index));
});
watch([folder, digits], () => {
  saveSettings();
  window.clearTimeout(numberTimer);
  numberTimer = window.setTimeout(() => updateNextNumber().catch((error) => {
    status.value = error instanceof Error ? error.message : '連番を取得できませんでした。';
  }), 350);
});

onMounted(() => {
  restoreSettings();
  selected.value = new Set(images.value.map((item) => item.index));
  void updateNextNumber().catch((error) => { status.value = error instanceof Error ? error.message : '連番を取得できませんでした。'; });
  navigator.permissions?.query({ name: 'clipboard-read' as PermissionName }).then((permission) => {
    if (permission.state !== 'granted') return;
    clipboardTimer = window.setInterval(() => { if (!document.hidden) void readClipboard(false); }, 1000);
    void readClipboard(false);
  }).catch(() => {});
});
onBeforeUnmount(() => {
  window.clearInterval(clipboardTimer);
  window.clearTimeout(numberTimer);
});
</script>

<template>
  <section class="instagram-body">
    <div>
      <div class="ig-form">
        <label for="vueIgUrl">URL</label>
        <input id="vueIgUrl" v-model="url" type="text" placeholder="x.com / instagram.com / threads.com のURL" @paste="onPaste">
        <button type="button" @click="readClipboard(true)">貼付</button>
        <button type="button" :disabled="fetching" @click="fetchImages">取得</button>
        <button v-if="fetching" type="button" @click="cancelFetch">キャンセル</button>
      </div>
      <div class="ig-options">
        <label>保存先フォルダー<input v-model="folder" type="text" placeholder="instagram"></label>
        <label>連番開始<input v-model.number="startNumber" type="number" min="1"></label>
        <label>桁数<input v-model.number="digits" type="number" min="1" max="6"></label>
      </div>
    </div>
    <div class="ig-grid">
      <div v-if="!images.length" class="empty-folder">Instagram / Threads / X のURLを入力して取得してください。</div>
      <article
        v-for="item in images" :key="item.index"
        class="ig-item" :class="{selected: selected.has(item.index), pending: !item.previewReady}"
        @click="toggle(item.index)"
      >
        <div class="ig-thumb">
          <img v-if="item.previewReady && item.previewUrl" :src="item.previewUrl" :alt="item.filename || String(item.index)">
          <span v-else>読込中...</span>
        </div>
        <label><input type="checkbox" :checked="selected.has(item.index)" @click.stop @change="toggle(item.index)"><span>{{ item.filename || item.index }}</span></label>
      </article>
    </div>
    <div class="ig-actions">
      <button type="button" @click="desktop.openUtility('instagram-auth')">Instagramログイン</button>
      <button type="button" @click="selectAll">全選択</button>
      <button type="button" @click="clearAll">全解除</button>
      <button type="button" @click="invert">反転</button>
      <button type="button" :disabled="saving || !readyImages.length" @click="saveImages">選択画像を保存</button>
      <span class="ig-status">{{ status }}</span>
    </div>
  </section>
</template>
