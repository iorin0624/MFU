<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { AlbumChild, MediaItem, Pagination } from '@/types';
import { formatBytes } from '@/utils/format';

const route = useRoute();
const router = useRouter();
const albumId = String(route.params.albumId);
const childId = String(route.params.childId);
const child = ref<AlbumChild | null>(null);
const media = ref<MediaItem[]>([]);
const pagination = ref<Pagination | null>(null);
const page = ref(1);
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const selected = ref<string[]>([]);
const fileInput = ref<HTMLInputElement | null>(null);
const allSelected = computed(() => media.value.length > 0 && media.value.every((item) => selected.value.includes(item.name)));

async function load(targetPage = page.value) {
  loading.value = true;
  error.value = '';
  try {
    const response = await portalApi.media(albumId, childId, targetPage);
    child.value = response.child;
    media.value = response.media;
    pagination.value = response.pagination;
    page.value = response.pagination.page;
    selected.value = [];
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '写真・動画を取得できませんでした。';
  } finally {
    loading.value = false;
  }
}

function toggle(name: string) {
  selected.value = selected.value.includes(name)
    ? selected.value.filter((value) => value !== name)
    : [...selected.value, name];
}

function toggleAll() {
  selected.value = allSelected.value ? [] : media.value.map((item) => item.name);
}

async function upload(files: FileList | null) {
  if (!files?.length) return;
  busy.value = true;
  error.value = '';
  try {
    await portalApi.uploadMedia(albumId, childId, Array.from(files));
    await load(page.value);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アップロードできませんでした。';
  } finally {
    busy.value = false;
    if (fileInput.value) fileInput.value.value = '';
  }
}

async function removeSelected() {
  if (!selected.value.length || !window.confirm(`${selected.value.length}件を削除しますか？`)) return;
  busy.value = true;
  error.value = '';
  try {
    await portalApi.deleteMedia(albumId, childId, selected.value);
    await load(page.value);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '削除できませんでした。';
  } finally {
    busy.value = false;
  }
}

onMounted(() => load());
</script>

<template>
  <button type="button" class="back-link" @click="router.push(`/albums/${albumId}`)">← アルバムへ</button>
  <LoadingBlock v-if="loading && !child">写真・動画を読み込んでいます</LoadingBlock>
  <div v-else-if="error && !child" class="alert error">{{ error }}</div>
  <template v-else-if="child">
    <section class="page-heading child-heading">
      <div>
        <p class="eyebrow">{{ child.mode === 'movie' ? 'MOVIES' : 'PHOTOS' }}</p>
        <h1>{{ child.name }}</h1>
        <p>{{ pagination?.total ?? child.mediaCount }}{{ child.mediaUnit }}</p>
      </div>
      <label v-if="child.permissions.canUpload" class="button primary upload-button">
        {{ busy ? '処理中…' : '＋ 追加' }}
        <input ref="fileInput" type="file" multiple :accept="child.mode === 'movie' ? 'video/*' : 'image/*'" :disabled="busy" @change="upload(($event.target as HTMLInputElement).files)">
      </label>
    </section>
    <div v-if="error" class="alert error compact-alert">{{ error }}</div>

    <div v-if="media.length" class="media-toolbar">
      <button type="button" class="button secondary compact" @click="toggleAll">{{ allSelected ? '全解除' : '全選択' }}</button>
      <span>{{ selected.length }}件選択中</span>
      <button v-if="child.permissions.canDeleteMedia" type="button" class="button danger compact" :disabled="!selected.length || busy" @click="removeSelected">削除</button>
    </div>

    <EmptyState v-if="!media.length" :icon="child.mode === 'movie' ? '🎬' : '🖼️'" title="まだファイルがありません" text="追加権限がある場合は、上の追加ボタンからアップロードできます。" />
    <div v-else class="media-grid" :aria-busy="loading || busy">
      <article v-for="item in media" :key="item.name" :class="['media-card', { selected: selected.includes(item.name) }]">
        <button type="button" class="select-media" :aria-pressed="selected.includes(item.name)" @click="toggle(item.name)">
          <span>{{ selected.includes(item.name) ? '✓' : '' }}</span>
        </button>
        <a class="media-preview" :href="item.viewUrl" target="_blank" rel="noopener">
          <img v-if="item.thumbnailUrl || item.posterUrl" :src="item.thumbnailUrl || item.posterUrl || ''" :alt="item.name" loading="lazy">
          <video v-else-if="item.kind === 'video'" :src="item.viewUrl" preload="metadata" muted></video>
          <span v-else class="media-placeholder">🖼️</span>
          <span v-if="item.kind === 'video'" class="video-badge">▶ 動画</span>
          <span v-if="item.converting" class="video-badge converting">変換中</span>
        </a>
        <div class="media-meta">
          <strong :title="item.name">{{ item.name }}</strong>
          <small>{{ formatBytes(item.size) }}</small>
          <a class="download-link" :href="item.downloadUrl" download>ダウンロード</a>
        </div>
      </article>
    </div>

    <nav v-if="pagination && pagination.pages > 1" class="pagination" aria-label="ページ移動">
      <button type="button" :disabled="!pagination.hasPrevious || loading" @click="load(page - 1)">前へ</button>
      <span>{{ page }} / {{ pagination.pages }}</span>
      <button type="button" :disabled="!pagination.hasNext || loading" @click="load(page + 1)">次へ</button>
    </nav>
  </template>
</template>
