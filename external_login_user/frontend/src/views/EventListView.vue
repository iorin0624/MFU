<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import EmptyState from '@/components/EmptyState.vue';
import { usePortalStore } from '@/stores/portal';
import { formatDateTime, formatMoney, membershipLabel } from '@/utils/format';

const store = usePortalStore();
const router = useRouter();
const scope = ref<'all' | 'upcoming' | 'past'>('upcoming');
const refreshing = ref(false);
const error = ref('');
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
        <div class="card-actions">
          <span>詳細を見る</span><span aria-hidden="true">→</span>
        </div>
      </div>
    </article>
  </div>
</template>
