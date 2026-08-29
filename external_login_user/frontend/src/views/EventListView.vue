<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import InAppBrowserAlbumNotice from '@/components/InAppBrowserAlbumNotice.vue';
import { usePortalStore } from '@/stores/portal';
import type { EventItem } from '@/types';
import { formatDateTime, formatMoney, membershipLabel } from '@/utils/format';
import { isInAppBrowser } from '@/utils/inAppBrowser';

const store = usePortalStore();
const router = useRouter();
const scope = ref<'all' | 'upcoming' | 'past'>('upcoming');
const refreshing = ref(false);
const error = ref('');
const albumNoticeEvent = ref<EventItem | null>(null);
const tipEvent = ref<EventItem | null>(null);
const tipAmount = ref(1000);
const scopes: Array<{ id: 'all' | 'upcoming' | 'past'; label: string }> = [
  { id: 'upcoming', label: '開催予定' },
  { id: 'past', label: '過去' },
  { id: 'all', label: 'すべて' },
];

const now = new Date();
const events = computed(() => {
  const rows = [...store.events];
  return rows.filter((event) => {
    if (scope.value === 'all') return true;
    if (!event.startsAt) return scope.value === 'upcoming';
    const date = new Date(event.startsAt);
    const upcoming = date.toDateString() === now.toDateString() || date >= now;
    return scope.value === 'upcoming' ? upcoming : !upcoming;
  }).sort((left, right) => {
    const a = left.startsAt ? new Date(left.startsAt).getTime() : Number.MAX_SAFE_INTEGER;
    const b = right.startsAt ? new Date(right.startsAt).getTime() : Number.MAX_SAFE_INTEGER;
    return scope.value === 'past' ? b - a : a - b;
  });
});
const unpaidEvents = computed(() => store.events.filter((event) => Boolean(
  event.membership
  && !event.membership.isCanceled
  && event.membership.status === 'approved'
  && event.membership.requirePayment
  && Number(event.feeYen || 0) > 0
  && event.membership.paymentStatus !== 'paid',
)));
const albumNoticeUrl = computed(() => albumNoticeEvent.value?.albumId
  ? new URL(router.resolve({ name: 'album', params: { albumId: albumNoticeEvent.value.albumId } }).href, window.location.origin).href
  : window.location.href);

function openAlbum(event: EventItem) {
  if (!event.albumId) return;
  if (isInAppBrowser()) albumNoticeEvent.value = event;
  else void router.push({ name: 'album', params: { albumId: event.albumId } });
}

async function selectScope(value: typeof scope.value) {
  scope.value = value;
  refreshing.value = true;
  error.value = '';
  try {
    await store.refreshEvents(value);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'イベント一覧を更新できませんでした。';
  } finally {
    refreshing.value = false;
  }
}
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">EVENTS</p>
      <h1>イベント</h1>
      <p>参加予定や過去のイベントを確認できます。</p>
    </div>
    <button class="button secondary compact" type="button" :disabled="refreshing" @click="selectScope(scope)">更新</button>
  </section>

  <div class="segmented" aria-label="表示するイベント">
    <button v-for="item in scopes"
      :key="item.id" type="button" :class="{ active: scope === item.id }" @click="selectScope(item.id)">
      {{ item.label }}
    </button>
  </div>
  <div v-if="error" class="alert error compact-alert">{{ error }}</div>
  <section v-if="unpaidEvents.length" class="unpaid-summary">
    <div><strong>💳 未払いのイベントが{{ unpaidEvents.length }}件あります</strong><span>まとめて確認できます。</span></div>
    <div class="unpaid-links"><a v-for="item in unpaidEvents" :key="item.uuid" :href="item.urls.payment">{{ item.title }}（{{ formatMoney(item.feeYen) }}）</a></div>
  </section>

  <EmptyState v-if="!events.length" icon="📅" title="表示できるイベントはありません" text="参加登録されたイベントがここに表示されます。" />
  <div v-else class="event-grid" :aria-busy="refreshing">
    <article v-for="event in events" :key="event.uuid" class="event-card" @click="router.push(`/events/${event.uuid}`)">
      <div class="event-date">
        <span>{{ event.startsAt ? new Date(event.startsAt).getDate() : '–' }}</span>
        <small>{{ event.startsAt ? `${new Date(event.startsAt).getMonth() + 1}月` : '未定' }}</small>
      </div>
      <div class="event-card-body">
        <div class="status-row">
          <span :class="['status-pill', event.membership?.status || 'admin']">
            {{ event.membership ? membershipLabel(event.membership.status, event.membership.isCanceled) : '管理権限' }}
          </span>
          <span v-if="event.membership?.paymentStatus === 'paid'" class="status-pill paid">入金済み</span>
        </div>
        <h2>{{ event.title }}</h2>
        <dl class="summary-list">
          <div><dt>日時</dt><dd>{{ formatDateTime(event.startsAt) }}</dd></div>
          <div v-if="event.placeName"><dt>場所</dt><dd>{{ event.placeName }}</dd></div>
          <div><dt>参加費</dt><dd>{{ formatMoney(event.feeYen) }}</dd></div>
        </dl>
        <div class="card-actions event-direct-actions" @click.stop>
          <a v-if="event.membership?.paymentStatus === 'paid' && event.urls.receipt" :href="event.urls.receipt" target="_blank" rel="noopener">レシート</a>
          <a v-else-if="event.membership?.requirePayment && Number(event.feeYen || 0) > 0 && event.membership.paymentStatus !== 'paid'" :href="event.urls.payment">支払</a>
          <button v-if="event.albumId && event.permissions.canOpenAlbum" type="button" @click="openAlbum(event)">アルバム</button>
          <button v-if="event.tipEnabled && event.membership && !event.membership.isCanceled" type="button" @click="tipEvent = event">投げ銭</button>
          <button type="button" @click="router.push(`/events/${event.uuid}`)">詳細</button>
        </div>
      </div>
    </article>
  </div>

  <div v-if="albumNoticeEvent" class="modal-backdrop" @click.self="albumNoticeEvent = null"><div class="modal-card inapp-album-modal"><InAppBrowserAlbumNotice :target-url="albumNoticeUrl" /><div class="modal-actions"><button type="button" class="button secondary" @click="albumNoticeEvent = null">閉じる</button></div></div></div>
  <div v-if="tipEvent" class="modal-backdrop" @click.self="tipEvent = null">
    <form class="modal-card" method="post" :action="tipEvent.urls.tip"><h2>{{ tipEvent.title }}へ投げ銭</h2><div class="alert warning compact-alert">投げ銭は返金できません。</div><input type="hidden" name="event_id" :value="tipEvent.id"><label>金額（円）<input v-model.number="tipAmount" type="number" name="amount_yen" min="100" max="100000" required></label><div class="tip-presets"><button v-for="amount in [500,1000,3000,5000]" :key="amount" type="button" @click="tipAmount = amount">¥{{ amount.toLocaleString() }}</button></div><div class="modal-actions"><button type="button" class="button secondary" @click="tipEvent = null">キャンセル</button><button type="submit" class="button primary">投げ銭する</button></div></form>
  </div>
</template>
