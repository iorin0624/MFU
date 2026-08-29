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
const showOpenchatDialog = ref(false);
const showTipDialog = ref(false);
const tipAmount = ref(1000);
const copied = ref('');
const participantsMailBusy = ref(false);
const actionMessage = ref('');
const albumUrl = computed(() => {
  if (!event.value?.albumId) return window.location.href;
  const path = router.resolve({ name: 'album', params: { albumId: event.value.albumId } }).href;
  return new URL(path, window.location.origin).href;
});
const needsPayment = computed(() => Boolean(
  event.value?.membership?.requirePayment
  && Number(event.value?.feeYen || 0) > 0
  && event.value?.membership?.paymentStatus !== 'paid',
));
const eventHasEnded = computed(() => Boolean(event.value?.startsAt && new Date(event.value.startsAt) < new Date()));

async function copyText(value: string, key: string) {
  await navigator.clipboard.writeText(value);
  copied.value = key;
  window.setTimeout(() => { copied.value = ''; }, 2000);
}

function openOpenchat() {
  if (!event.value?.lineOpenchatUrl) return;
  if (event.value.lineOpenchatPass) showOpenchatDialog.value = true;
  else window.open(event.value.lineOpenchatUrl, '_blank', 'noopener');
}

async function requestParticipantsEmail() {
  if (!event.value) return;
  participantsMailBusy.value = true;
  actionMessage.value = '';
  try {
    const response = await portalApi.requestParticipantsEmail(event.value.uuid);
    actionMessage.value = response.message;
  } catch (reason) {
    actionMessage.value = reason instanceof Error ? reason.message : 'メール送信を受け付けできませんでした。';
  } finally {
    participantsMailBusy.value = false;
  }
}

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
          <div v-if="event.payFrom"><dt>支払開始</dt><dd>{{ formatDateTime(event.payFrom) }}</dd></div>
          <div v-if="event.payUntil"><dt>支払終了</dt><dd>{{ formatDateTime(event.payUntil) }}</dd></div>
        </dl>
        <div class="inline-actions">
          <a v-if="event.mapsUrl" class="button secondary" :href="event.mapsUrl" target="_blank" rel="noopener">地図を開く</a>
          <a v-if="event.googleFormUrl" class="button secondary" :href="event.googleFormUrl" target="_blank" rel="noopener">案内フォーム</a>
          <button v-if="event.snsHashtag" type="button" class="button secondary" @click="copyText(`#${event.snsHashtag.replace(/^#/, '')}`, 'hashtag')">{{ copied === 'hashtag' ? 'コピー済み' : 'ハッシュタグをコピー' }}</button>
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
        <button v-if="event.lineOpenchatUrl && event.permissions.canOpenChat" class="feature-link chat" type="button" @click="openOpenchat">
          <span class="feature-icon">💬</span><span><strong>LINEオープンチャット</strong><small>{{ event.lineOpenchatPass ? 'パスコードを確認して参加' : '連絡用チャットを開く' }}</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canViewMembers" class="feature-link members" type="button" @click="router.push({ name: 'event-social', params: { uuid: event.uuid } })">
          <span class="feature-icon">🌐</span><span><strong>SNSリンク・貼付用</strong><small>X / Instagramの一覧とコピー</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canRequestParticipantsPngEmail" class="feature-link members" type="button" :disabled="participantsMailBusy" @click="requestParticipantsEmail">
          <span class="feature-icon">🖼️</span><span><strong>参加者一覧PNG</strong><small>確認済みメールアドレスで受け取る</small></span><b>›</b>
        </button>
        <a v-if="needsPayment" class="feature-link payment" :href="event.urls.payment">
          <span class="feature-icon">💳</span><span><strong>お支払い</strong><small>参加費を確認する</small></span><b>›</b>
        </a>
        <a v-if="event.membership?.paymentStatus === 'paid' && event.urls.receipt" class="feature-link payment" :href="event.urls.receipt" target="_blank" rel="noopener">
          <span class="feature-icon">🧾</span><span><strong>支払済みレシート</strong><small>PDFを開く</small></span><b>›</b>
        </a>
        <button v-if="event.tipEnabled && eventHasEnded && event.membership && !event.membership.isCanceled" class="feature-link tip" type="button" @click="showTipDialog = true">
          <span class="feature-icon">🎁</span><span><strong>投げ銭</strong><small>Squareで主催者を応援する</small></span><b>›</b>
        </button>
        <div v-if="actionMessage" class="inline-notice">{{ actionMessage }}</div>
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
          <div v-if="event.membership.checkinMethodLabel"><dt>受付方法</dt><dd>{{ event.membership.checkinMethodLabel }}</dd></div>
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

  <div v-if="showOpenchatDialog && event" class="modal-backdrop" @click.self="showOpenchatDialog = false">
    <div class="modal-card">
      <h2>オープンチャット参加前の確認</h2>
      <p>下のパスコードを控えてから参加してください。</p>
      <button type="button" class="openchat-pass" @click="copyText(event.lineOpenchatPass || '', 'openchat')">{{ event.lineOpenchatPass }}<small>{{ copied === 'openchat' ? 'コピーしました' : 'タップしてコピー' }}</small></button>
      <div class="modal-actions"><button type="button" class="button secondary" @click="showOpenchatDialog = false">閉じる</button><a class="button primary" :href="event.lineOpenchatUrl || '#'" target="_blank" rel="noopener">参加する</a></div>
    </div>
  </div>

  <div v-if="showTipDialog && event" class="modal-backdrop" @click.self="showTipDialog = false">
    <form class="modal-card" method="post" :action="event.urls.tip">
      <h2>投げ銭</h2><div class="alert warning compact-alert">投げ銭は返金できません。</div>
      <input type="hidden" name="event_id" :value="event.id">
      <label>金額（円）<input v-model.number="tipAmount" type="number" name="amount_yen" min="100" max="100000" step="1" required></label>
      <div class="tip-presets"><button v-for="amount in [500,1000,3000,5000]" :key="amount" type="button" @click="tipAmount = amount">¥{{ amount.toLocaleString() }}</button></div>
      <div class="modal-actions"><button type="button" class="button secondary" @click="showTipDialog = false">キャンセル</button><button type="submit" class="button primary">投げ銭する</button></div>
    </form>
  </div>
</template>
