<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { portalApi } from '@/api/client';
import { formatDateTime } from '@/utils/format';
import { usePortalStore } from '@/stores/portal';
import { useRouter } from 'vue-router';

type Item = { id: number; kind?: string; title?: string; body: string; target_url: string; room_name?: string; created_at?: string; read_at?: string };
const items = ref<Item[]>([]);
const page = ref(1);
const hasNext = ref(false);
const filter = ref<'all' | 'unread' | 'notice' | 'chat'>('all');
const busy = ref(false);
const error = ref('');
const loadSentinel = ref<HTMLElement | null>(null);
let observer: IntersectionObserver | null = null;
const store = usePortalStore();
const router = useRouter();
const unreadOnly = computed(() => filter.value === 'unread');
const category = computed<'all' | 'notice' | 'chat'>(() => (
  filter.value === 'notice' || filter.value === 'chat' ? filter.value : 'all'
));
const counts = computed(() => store.session?.unread || { total: 0, notifications: 0, chat: 0 });

function notificationScope(): 'external' | 'mfu' {
  return store.session?.notificationScope === 'mfu' ? 'mfu' : 'external';
}

async function load(reset = true) {
  busy.value = true; error.value = '';
  try {
    const response = await portalApi.notifications(notificationScope(), reset ? 1 : page.value, unreadOnly.value, category.value);
    items.value = reset ? response.items : [...items.value, ...response.items];
    page.value = response.pagination.page ?? (reset ? 1 : page.value);
    hasNext.value = response.pagination.has_next;
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '通知を取得できませんでした。'; }
  finally { busy.value = false; }
}
async function open(item: Item) {
  if (!item.read_at) {
    await portalApi.markNotificationRead(notificationScope(), item.id);
    item.read_at = new Date().toISOString();
    await store.refreshUnread();
  }
  const target = item.target_url || '/external-login/app/';
  const parsed = new URL(target, window.location.origin);
  const eventMatch = parsed.pathname.match(/^\/chat\/events\/(\d+)/);
  if (eventMatch) {
    const event = store.events.find((entry) => entry.id === Number(eventMatch[1]));
    if (event) {
      await router.push({ name: 'event-chat', params: { uuid: event.uuid }, query: { room_id: parsed.searchParams.get('room_id') || undefined } });
      return;
    }
  }
  const dmMatch = parsed.pathname.match(/^\/chat\/dm\/room\/([^/]+)/);
  if (dmMatch) {
    await router.push({ name: 'chat-dm', params: { dmUuid: dmMatch[1] } });
    return;
  }
  const eventAlbumMatch = parsed.pathname.match(/^\/external-login\/events\/([^/]+)\/album\/?$/);
  if (eventAlbumMatch) {
    const event = store.events.find((entry) => entry.uuid === eventAlbumMatch[1]);
    if (event?.albumId) {
      const childId = parsed.searchParams.get('child_id');
      await router.push(childId
        ? { name: 'album-child', params: { albumId: event.albumId, childId } }
        : { name: 'album', params: { albumId: event.albumId } });
      return;
    }
  }
  const legacyAlbumMatch = parsed.pathname.match(/^\/album\/([^/]+)\/view\/([^/]+)\/?$/);
  if (legacyAlbumMatch) {
    await router.push({ name: 'album-child', params: { albumId: legacyAlbumMatch[1], childId: legacyAlbumMatch[2] } });
    return;
  }
  window.location.assign(target);
}
async function readAll() {
  if (!window.confirm('通常通知をすべて既読にします。\n但し、未読チャットは既読にはなりません。')) return;
  busy.value = true; error.value = '';
  try {
    await portalApi.markAllNotificationsRead(notificationScope());
    await store.refreshUnread();
    await load(true);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '通知を既読にできませんでした。';
  } finally {
    busy.value = false;
  }
}
function setFilter(value: typeof filter.value) {
  if (filter.value === value || busy.value) return;
  filter.value = value;
  page.value = 1;
  void load(true);
}
async function loadMore() {
  if (busy.value || !hasNext.value) return;
  page.value += 1;
  await load(false);
}
onMounted(async () => {
  await load();
  await nextTick();
  observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) void loadMore();
  }, { rootMargin: '240px 0px' });
  if (loadSentinel.value) observer.observe(loadSentinel.value);
});
onBeforeUnmount(() => observer?.disconnect());
watch(() => store.session?.unread.total || 0, (value, previous) => {
  if (value > previous) void load(true);
});
</script>

<template>
  <section class="page-heading"><div><p class="eyebrow">NOTIFICATIONS</p><h1>通知</h1><p>イベントやチャットの更新を確認できます。</p></div><div class="heading-actions"><button class="button secondary compact" type="button" :disabled="busy" @click="readAll">すべて既読</button></div></section>
  <div class="segmented notification-filters" aria-label="通知の絞り込み">
    <button :class="{ active: filter === 'all' }" @click="setFilter('all')">すべて <span v-if="counts.total">{{ counts.total > 99 ? '99+' : counts.total }}</span></button>
    <button :class="{ active: filter === 'unread' }" @click="setFilter('unread')">未読 <span v-if="counts.total">{{ counts.total > 99 ? '99+' : counts.total }}</span></button>
    <button :class="{ active: filter === 'notice' }" @click="setFilter('notice')">お知らせ <span v-if="counts.notifications">{{ counts.notifications > 99 ? '99+' : counts.notifications }}</span></button>
    <button :class="{ active: filter === 'chat' }" @click="setFilter('chat')">チャット <span v-if="counts.chat">{{ counts.chat > 99 ? '99+' : counts.chat }}</span></button>
  </div>
  <div v-if="error" class="alert error">{{ error }}</div>
  <div v-if="items.length" class="notification-list">
    <button v-for="item in items" :key="item.id" type="button" :class="['notification-card', { unread: !item.read_at }]" @click="open(item)">
      <span class="notification-dot"></span><span><strong>{{ item.title || 'お知らせ' }}</strong><small v-if="item.room_name">{{ item.room_name }}</small><p>{{ item.body }}</p><time>{{ formatDateTime(item.created_at) }}</time></span><b>›</b>
    </button>
  </div>
  <div v-else-if="!busy" class="empty-inline">表示する通知はありません。</div>
  <div ref="loadSentinel" class="notification-load-sentinel" aria-hidden="true"></div>
  <div v-if="busy && items.length" class="empty-inline">続きを読み込んでいます…</div>
</template>
