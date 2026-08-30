<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { portalApi } from '@/api/client';
import { formatDateTime } from '@/utils/format';

type Item = { id: number; kind?: string; title?: string; body: string; target_url: string; room_name?: string; created_at?: string; read_at?: string };
const items = ref<Item[]>([]);
const page = ref(1);
const hasNext = ref(false);
const unreadOnly = ref(false);
const busy = ref(false);
const error = ref('');

async function load(reset = true) {
  busy.value = true; error.value = '';
  try {
    const response = await portalApi.notifications(reset ? 1 : page.value, unreadOnly.value);
    items.value = reset ? response.items : [...items.value, ...response.items];
    page.value = response.pagination.page;
    hasNext.value = response.pagination.has_next;
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '通知を取得できませんでした。'; }
  finally { busy.value = false; }
}
async function open(item: Item) {
  if (!item.read_at) { await portalApi.markNotificationRead(item.id); item.read_at = new Date().toISOString(); }
  window.location.assign(item.target_url || '/external-login/vue-preview/');
}
async function readAll() { await portalApi.markAllNotificationsRead(); items.value.forEach((item) => { item.read_at ||= new Date().toISOString(); }); }
onMounted(() => load());
</script>

<template>
  <section class="page-heading"><div><p class="eyebrow">NOTIFICATIONS</p><h1>通知</h1><p>イベントやチャットの更新を確認できます。</p></div><button class="button secondary compact" type="button" @click="readAll">すべて既読</button></section>
  <div class="segmented"><button :class="{ active: !unreadOnly }" @click="unreadOnly = false; load()">すべて</button><button :class="{ active: unreadOnly }" @click="unreadOnly = true; load()">未読のみ</button></div>
  <div v-if="error" class="alert error">{{ error }}</div>
  <div v-if="items.length" class="notification-list">
    <button v-for="item in items" :key="item.id" type="button" :class="['notification-card', { unread: !item.read_at }]" @click="open(item)">
      <span class="notification-dot"></span><span><strong>{{ item.title || 'お知らせ' }}</strong><small v-if="item.room_name">{{ item.room_name }}</small><p>{{ item.body }}</p><time>{{ formatDateTime(item.created_at) }}</time></span><b>›</b>
    </button>
  </div>
  <div v-else-if="!busy" class="empty-inline">表示する通知はありません。</div>
  <button v-if="hasNext" class="button secondary wide" :disabled="busy" @click="page += 1; load(false)">続きを読み込む</button>
</template>
