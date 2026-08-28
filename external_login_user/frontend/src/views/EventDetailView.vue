<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import InAppBrowserAlbumNotice from '@/components/InAppBrowserAlbumNotice.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { EventItem } from '@/types';
import { formatDateTime, formatMoney, membershipLabel } from '@/utils/format';
import { isInAppBrowser } from '@/utils/inAppBrowser';

const route = useRoute();
const router = useRouter();
const event = ref<EventItem | null>(null);
const loading = ref(true);
const error = ref('');
const showInAppAlbumNotice = ref(false);
const albumUrl = computed(() => {
  if (!event.value?.albumId) return window.location.href;
  const path = router.resolve({ name: 'album', params: { albumId: event.value.albumId } }).href;
  return new URL(path, window.location.origin).href;
});

function openAlbum() {
  if (!event.value?.albumId) return;
  if (isInAppBrowser()) {
    showInAppAlbumNotice.value = true;
    return;
  }
  void router.push({ name: 'album', params: { albumId: event.value.albumId } });
}

onMounted(async () => {
  try {
    event.value = (await portalApi.event(String(route.params.uuid))).event;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'イベントを取得できませんでした。';
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <button type="button" class="back-link" @click="router.push('/')">← イベント一覧へ</button>
  <LoadingBlock v-if="loading">イベント詳細を読み込んでいます</LoadingBlock>
  <div v-else-if="error" class="alert error">{{ error }}</div>
  <template v-else-if="event">
    <section class="event-hero">
      <div class="status-row">
        <span :class="['status-pill', event.membership?.status || 'admin']">
          {{ event.membership ? membershipLabel(event.membership.status, event.membership.isCanceled) : '管理権限' }}
        </span>
        <span v-if="event.membership?.paymentStatus === 'paid'" class="status-pill paid">入金済み</span>
      </div>
      <p class="eyebrow">EVENT DETAIL</p>
      <h1>{{ event.title }}</h1>
      <p class="hero-date">{{ formatDateTime(event.startsAt) }}</p>
    </section>

    <div class="detail-layout">
      <section class="panel">
        <h2>イベント情報</h2>
        <dl class="detail-list">
          <div><dt>開催日時</dt><dd>{{ formatDateTime(event.startsAt) }}</dd></div>
          <div><dt>会場</dt><dd>{{ event.placeName || '未設定' }}</dd></div>
          <div v-if="event.address"><dt>住所</dt><dd>{{ event.address }}</dd></div>
          <div><dt>参加費</dt><dd>{{ formatMoney(event.feeYen) }}</dd></div>
          <div v-if="event.snsHashtag"><dt>ハッシュタグ</dt><dd>#{{ event.snsHashtag.replace(/^#/, '') }}</dd></div>
        </dl>
        <div class="inline-actions">
          <a v-if="event.mapsUrl" class="button secondary" :href="event.mapsUrl" target="_blank" rel="noopener">地図を開く</a>
          <a v-if="event.googleFormUrl" class="button secondary" :href="event.googleFormUrl" target="_blank" rel="noopener">案内フォーム</a>
          <a v-if="event.lineOpenchatUrl" class="button secondary" :href="event.lineOpenchatUrl" target="_blank" rel="noopener">オープンチャット</a>
        </div>
      </section>

      <section class="panel action-panel">
        <h2>イベントメニュー</h2>
        <button v-if="event.permissions.canOpenAlbum && event.albumId" class="feature-link album" type="button" @click="openAlbum">
          <span class="feature-icon">📷</span><span><strong>アルバム</strong><small>写真・動画を見る</small></span><b>›</b>
        </button>
        <a v-if="event.permissions.canOpenChat" class="feature-link chat" :href="event.urls.chat">
          <span class="feature-icon">💬</span><span><strong>チャット</strong><small>参加者とやり取りする</small></span><b>›</b>
        </a>
        <a v-if="event.permissions.canViewMembers" class="feature-link members" :href="event.urls.members">
          <span class="feature-icon">👥</span><span><strong>参加者</strong><small>参加メンバーを見る</small></span><b>›</b>
        </a>
        <a v-if="event.membership?.requirePayment && event.membership.paymentStatus !== 'paid'" class="feature-link payment" :href="event.urls.payment">
          <span class="feature-icon">💳</span><span><strong>お支払い</strong><small>参加費を確認する</small></span><b>›</b>
        </a>
        <div v-if="!event.permissions.canOpenAlbum && event.albumId" class="inline-notice">アルバムは参加承認後に閲覧できます。</div>
      </section>

      <section v-if="event.membership" class="panel membership-panel">
        <h2>参加状況</h2>
        <dl class="detail-list">
          <div><dt>承認状態</dt><dd>{{ membershipLabel(event.membership.status, event.membership.isCanceled) }}</dd></div>
          <div><dt>支払状態</dt><dd>{{ event.membership.paymentStatus === 'paid' ? '入金済み' : '未入金' }}</dd></div>
          <div v-if="event.membership.participantRole !== 'none'"><dt>参加区分</dt><dd>{{ event.membership.participantRole }}</dd></div>
          <div v-if="event.membership.costumeLabel"><dt>内容</dt><dd>{{ event.membership.costumeLabel }}</dd></div>
          <div><dt>受付状態</dt><dd>{{ event.membership.checkinAt ? '受付済み' : '未受付' }}</dd></div>
          <div v-if="event.membership.checkinAt"><dt>受付日時</dt><dd>{{ formatDateTime(event.membership.checkinAt) }}</dd></div>
        </dl>
        <button
          v-if="event.permissions.canOpenPass"
          type="button"
          class="button primary participant-pass-button"
          @click="router.push({ name: 'event-pass', params: { uuid: event.uuid } })"
        >🎫 参加証を開く</button>
      </section>
    </div>
  </template>

  <div v-if="showInAppAlbumNotice" class="modal-backdrop" @click.self="showInAppAlbumNotice = false">
    <div class="modal-card inapp-album-modal">
      <InAppBrowserAlbumNotice :target-url="albumUrl" />
      <div class="modal-actions">
        <button type="button" class="button secondary" @click="showInAppAlbumNotice = false">閉じる</button>
      </div>
    </div>
  </div>
</template>
