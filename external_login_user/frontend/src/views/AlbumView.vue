<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { AlbumChild, AlbumItem } from '@/types';

const route = useRoute();
const router = useRouter();
const albumId = String(route.params.albumId);
const album = ref<AlbumItem | null>(null);
const children = ref<AlbumChild[]>([]);
const loading = ref(true);
const saving = ref(false);
const error = ref('');
const dialog = ref<'create' | 'rename' | null>(null);
const target = ref<AlbumChild | null>(null);
const childName = ref('');
const childMode = ref<AlbumChild['mode']>('normal');

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [albumResponse, childResponse] = await Promise.all([
      portalApi.album(albumId), portalApi.children(albumId),
    ]);
    album.value = albumResponse.album;
    children.value = childResponse.children;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'アルバムを取得できませんでした。';
  } finally {
    loading.value = false;
  }
}

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
    } else if (target.value) {
      const response = await portalApi.renameChild(albumId, target.value.id, name);
      const index = children.value.findIndex((child) => child.id === target.value?.id);
      if (index >= 0) children.value[index] = response.child;
    }
    dialog.value = null;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '保存できませんでした。';
  } finally {
    saving.value = false;
  }
}

async function deleteChild(child: AlbumChild) {
  if (!window.confirm(`「${child.name}」を削除しますか？\n保存されている写真・動画も削除されます。この操作は元に戻せません。`)) return;
  error.value = '';
  try {
    await portalApi.deleteChild(albumId, child.id);
    children.value = children.value.filter((item) => item.id !== child.id);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '削除できませんでした。';
  }
}

onMounted(load);
</script>

<template>
  <button type="button" class="back-link" @click="router.back()">← イベント詳細へ</button>
  <LoadingBlock v-if="loading">アルバムを読み込んでいます</LoadingBlock>
  <div v-else-if="error && !album" class="alert error">{{ error }}</div>
  <template v-else-if="album">
    <section class="page-heading album-heading">
      <div>
        <p class="eyebrow">ALBUM</p>
        <h1>{{ album.name }}</h1>
        <p v-if="album.event">{{ album.event.title }}</p>
      </div>
      <button v-if="album.permissions.canCreateChild" type="button" class="button primary" @click="openCreate">＋ 子アルバム</button>
    </section>
    <div v-if="error" class="alert error compact-alert">{{ error }}</div>
    <div class="permission-note">
      <span>🔒</span>
      <span>自分で作成した子アルバムだけ、写真・動画の追加や削除ができます。</span>
    </div>

    <EmptyState v-if="!children.length" icon="📂" title="子アルバムはまだありません" text="作成権限がある場合は、新しい子アルバムを追加できます。" />
    <div v-else class="child-grid">
      <article v-for="child in children" :key="child.id" class="child-card">
        <button class="child-open" type="button" @click="router.push(`/albums/${albumId}/children/${child.id}`)">
          <span :class="['folder-icon', child.mode]">{{ child.mode === 'movie' ? '🎬' : child.mode === 'process' ? '🎨' : '📁' }}</span>
          <span class="child-info">
            <strong>{{ child.name }}</strong>
            <small>{{ child.mediaCount }}{{ child.mediaUnit }}</small>
          </span>
          <span aria-hidden="true">›</span>
        </button>
        <div v-if="child.permissions.createdByCurrentUser" class="owner-label">自分が作成</div>
        <div v-if="child.permissions.canRenameChild || child.permissions.canDeleteChild" class="child-actions">
          <button v-if="child.permissions.canRenameChild" type="button" @click="openRename(child)">名前変更</button>
          <button v-if="child.permissions.canDeleteChild" type="button" class="danger-text" @click="deleteChild(child)">削除</button>
        </div>
      </article>
    </div>
  </template>

  <div v-if="dialog" class="modal-backdrop" @click.self="dialog = null">
    <form class="modal-card" @submit.prevent="saveDialog">
      <h2>{{ dialog === 'create' ? '子アルバムを作成' : '子アルバム名を変更' }}</h2>
      <label>名前<input v-model="childName" required maxlength="120" autofocus></label>
      <label v-if="dialog === 'create'">種類
        <select v-model="childMode">
          <option value="normal">写真</option>
          <option value="movie">動画</option>
          <option value="process">加工用</option>
        </select>
      </label>
      <div class="modal-actions">
        <button type="button" class="button secondary" @click="dialog = null">キャンセル</button>
        <button type="submit" class="button primary" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
      </div>
    </form>
  </div>
</template>
