<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { runtimeConfig } from '@/config';
import { requestJson } from '@/api/client';
import { useDesktopStore } from '@/stores/desktop';
import { useDownloaderStore } from '@/stores/downloader';
import { useExplorerStore } from '@/stores/explorer';
import type { DownloadImageItem, DownloadVideoItem } from '@/types';
import { delay, extractSupportedMediaUrls } from '@/utils/mediaUrls';

const desktop = useDesktopStore();
const downloader = useDownloaderStore();
const explorer = useExplorerStore();
const url = ref(downloader.videoUrl);
const folder = ref('video');
const selected = ref(new Set<number>());
const status = ref('');
const fetching = ref(false);
const frameFetching = ref(false);
const saving = ref(false);
let clipboardTimer = 0;

function restoreSettings() {
  try {
    const settings = JSON.parse(localStorage.getItem('mfu.imageViewer.videoDownloaderSettings') || '{}');
    folder.value = settings.folder || 'video';
  } catch { /* optional */ }
}
function saveSettings() {
  localStorage.setItem('mfu.imageViewer.videoDownloaderSettings', JSON.stringify({ folder: folder.value || 'video' }));
}

function applyClipboardText(text: string, announce = false) {
  const next = extractSupportedMediaUrls(text)[0];
  if (!next) {
    if (announce) status.value = 'クリップボードにInstagram / Threads / X のURLがありません。';
    return false;
  }
  url.value = next;
  downloader.videoUrl = next;
  if (announce) status.value = `${next} をクリップボードから入力しました。`;
  return true;
}
async function readClipboard(announce = true) {
  try { applyClipboardText(await navigator.clipboard.readText(), announce); }
  catch { if (announce) status.value = 'クリップボードを読み取れません。URLを貼り付けてください。'; }
}
function onPaste(event: ClipboardEvent) {
  const text = event.clipboardData?.getData('text/plain') || '';
  if (!extractSupportedMediaUrls(text).length) return;
  event.preventDefault();
  applyClipboardText(text, true);
}

async function pollVideoJob(jobId: string) {
  const endpoint = runtimeConfig.videoJobUrl.replace('__JOB_ID__', encodeURIComponent(jobId));
  const started = Date.now();
  let count = 0;
  while (Date.now() - started < 15 * 60 * 1000) {
    await delay(count < 3 ? 700 : 1200);
    count += 1;
    const job = await requestJson<{
      ok: boolean; status?: string; identifier?: string; videos?: DownloadVideoItem[];
      loginRequired?: boolean; error?: string;
    }>(endpoint);
    if (job.status === 'login_required' || job.loginRequired) {
      desktop.openUtility('instagram-auth');
      throw new Error(job.error || 'Instagramの再ログインが必要です。VNCでOTPを入力してください。');
    }
    status.value = `投稿動画を取得中... ${count}`;
    if (job.status === 'done') return job;
  }
  throw new Error('取得がタイムアウトしました。もう一度お試しください。');
}

async function pollImageJob(jobId: string) {
  const endpoint = runtimeConfig.instagramJobUrl.replace('__JOB_ID__', encodeURIComponent(jobId));
  const started = Date.now();
  while (Date.now() - started < 15 * 60 * 1000) {
    await delay(900);
    const job = await requestJson<{
      ok: boolean; status?: string; shortcode?: string; images?: DownloadImageItem[];
      total?: number; processed?: number; loginRequired?: boolean; error?: string;
    }>(endpoint);
    if (job.status === 'login_required' || job.loginRequired) {
      desktop.openUtility('instagram-auth');
      throw new Error(job.error || 'Instagramの再ログインが必要です。VNCでOTPを入力してください。');
    }
    status.value = job.total
      ? `動画を写真に変換中... ${job.processed || 0}/${job.total}`
      : '動画を取得中...';
    if (job.status === 'done') return job;
  }
  throw new Error('取得がタイムアウトしました。もう一度お試しください。');
}

async function fetchVideos() {
  fetching.value = true;
  downloader.setVideos({ url: url.value, videos: [] });
  selected.value = new Set();
  status.value = '取得中...';
  try {
    const data = await requestJson<{ok: boolean; jobId?: string; identifier?: string; videos?: DownloadVideoItem[]}>(
      runtimeConfig.videoFetchUrl,
      { method: 'POST', body: JSON.stringify({ url: url.value.trim() }) },
    );
    const result = data.jobId ? await pollVideoJob(data.jobId) : data;
    const videos = result.videos || [];
    downloader.setVideos({
      url: url.value, identifier: result.identifier || data.identifier || '',
      jobId: data.jobId || '', videos,
    });
    selected.value = new Set(videos.map((item) => item.index));
    saveSettings();
    status.value = `${videos.length}件取得しました。`;
  } catch (error) {
    downloader.setVideos({ url: url.value, videos: [] });
    status.value = error instanceof Error ? error.message : '取得に失敗しました。';
  } finally { fetching.value = false; }
}

async function fetchFrames() {
  frameFetching.value = true;
  status.value = '動画を写真に変換しています...';
  try {
    const data = await requestJson<{ok: boolean; jobId?: string; shortcode?: string; images?: DownloadImageItem[]}>(
      runtimeConfig.videoFramesFetchUrl,
      { method: 'POST', body: JSON.stringify({ url: url.value.trim() }) },
    );
    const result = data.jobId ? await pollImageJob(data.jobId) : data;
    const images = result.images || [];
    downloader.setImages({
      url: url.value, identifier: result.shortcode || data.shortcode || '',
      jobId: data.jobId || '', images,
    });
    desktop.openUtility('image-downloader');
    status.value = `${images.length}枚を写真化し、画像取得ウィンドウに表示しました。`;
  } catch (error) {
    status.value = error instanceof Error ? error.message : '動画を写真で取得できませんでした。';
  } finally { frameFetching.value = false; }
}

function toggle(index: number) {
  const next = new Set(selected.value);
  if (next.has(index)) next.delete(index); else next.add(index);
  selected.value = next;
}
function selectAll() { selected.value = new Set(downloader.videos.map((item) => item.index)); }
function clearAll() { selected.value = new Set(); }
function invert() {
  selected.value = new Set(downloader.videos.filter((item) => !selected.value.has(item.index)).map((item) => item.index));
}

async function refreshExplorers() {
  const entries = desktop.windows.filter((win) => win.explorer);
  await Promise.all(entries.map((win) => explorer.refreshFolder(win.explorer!.folder)));
}

async function saveVideos() {
  if (!selected.value.size) { status.value = '保存する動画を選択してください。'; return; }
  saving.value = true;
  status.value = '保存中...';
  try {
    const data = await requestJson<{
      ok: boolean; saved?: unknown[]; duplicates?: Array<{existing?: {path?: string}}>;
    }>(runtimeConfig.videoSaveUrl, {
      method: 'POST',
      body: JSON.stringify({
        jobId: downloader.videoJobId, videos: downloader.videos,
        selected: [...selected.value], folder: folder.value,
      }),
    });
    const duplicates = data.duplicates || [];
    const paths = duplicates.slice(0, 3).map((row) => row.existing?.path).filter(Boolean);
    status.value = `${data.saved?.length || 0}件保存しました。${duplicates.length ? ` 重複${duplicates.length}件は保存しませんでした。${paths.length ? ` 既存: ${paths.join(', ')}` : ''}` : ''}`;
    saveSettings();
    await refreshExplorers();
  } catch (error) {
    status.value = error instanceof Error ? error.message : '保存に失敗しました。';
  } finally { saving.value = false; }
}

watch(url, (value) => { downloader.videoUrl = value; });
watch(folder, saveSettings);
onMounted(() => {
  restoreSettings();
  selected.value = new Set(downloader.videos.map((item) => item.index));
  navigator.permissions?.query({ name: 'clipboard-read' as PermissionName }).then((permission) => {
    if (permission.state !== 'granted') return;
    clipboardTimer = window.setInterval(() => { if (!document.hidden) void readClipboard(false); }, 1000);
    void readClipboard(false);
  }).catch(() => {});
});
onBeforeUnmount(() => window.clearInterval(clipboardTimer));
</script>

<template>
  <section class="instagram-body">
    <div>
      <div class="ig-form video-fetch-form">
        <label for="vueVdUrl">URL</label>
        <input id="vueVdUrl" v-model="url" type="text" placeholder="x.com / instagram.com / threads.com のURL" @paste="onPaste">
        <button type="button" @click="readClipboard(true)">貼付</button>
        <button type="button" :disabled="fetching || frameFetching" @click="fetchVideos">取得</button>
        <button type="button" :disabled="fetching || frameFetching" @click="fetchFrames">動画を写真で取得</button>
      </div>
      <div class="ig-options single-option">
        <label>保存先フォルダー<input v-model="folder" type="text" placeholder="video"></label>
      </div>
    </div>
    <div class="ig-grid">
      <div v-if="!downloader.videos.length" class="empty-folder">Instagram / Threads / X のURLを入力して動画を取得してください。</div>
      <article
        v-for="item in downloader.videos" :key="item.index"
        class="ig-item" :class="{selected: selected.has(item.index)}" @click="toggle(item.index)"
      >
        <div class="video-download-thumb">VIDEO</div>
        <label><input type="checkbox" :checked="selected.has(item.index)" @click.stop @change="toggle(item.index)"><span>{{ item.filename || item.index }}</span></label>
      </article>
    </div>
    <div class="ig-actions">
      <button type="button" @click="desktop.openUtility('instagram-auth')">Instagramログイン</button>
      <button type="button" @click="selectAll">全選択</button>
      <button type="button" @click="clearAll">全解除</button>
      <button type="button" @click="invert">反転</button>
      <button type="button" :disabled="saving || !downloader.videos.length" @click="saveVideos">選択動画を保存</button>
      <span class="ig-status">{{ status }}</span>
    </div>
  </section>
</template>
