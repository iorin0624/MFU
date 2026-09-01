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
const participantRole = ref('none');
const costumeLabel = ref('');
const processRequired = ref(false);
const participantSaving = ref(false);
const participantMessage = ref('');
const roleUsesMemo = computed(() => ['cosplayer', 'other'].includes(participantRole.value));
const restriction = computed(() => {
  const membership = event.value?.membership;
  if (!membership) return null;
  if (membership.isCanceled) return { tone: 'warning', title: '参加はキャンセル済みです', body: '参加者向けのチャット、アルバム、参加者一覧、参加証、アンケートは利用できません。支払済みのレシートは引き続き確認できます。再参加については主催者へお問い合わせください。' };
  if (membership.status === 'pending') return { tone: 'warning', title: '参加申請は承認待ちです', body: '主催者の承認が完了するまで、チャット、アルバム、参加者一覧、参加証、アンケートは利用できません。承認後に自動で利用可能になります。' };
  if (membership.status === 'rejected') return { tone: 'error', title: '参加申請は承認されませんでした', body: 'このイベントの参加者向け機能は利用できません。申請内容について確認が必要な場合は主催者へお問い合わせください。' };
  return null;
});
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

function openChat() {
  if (!event.value?.permissions.canOpenChat || event.value.lineOpenchatUrl) return;
  void router.push({ name: 'event-chat', params: { uuid: event.value.uuid } });
}

async function saveParticipantDetails() {
  if (!event.value) return;
  participantSaving.value = true; participantMessage.value = '';
  try {
    const response = await portalApi.saveMyEventRole(event.value.uuid, participantRole.value, roleUsesMemo.value ? costumeLabel.value : '');
    participantRole.value = response.participantRole;
    costumeLabel.value = response.costumeLabel || '';
    if (event.value.membership) {
      event.value.membership.participantRole = response.participantRole;
      event.value.membership.costumeLabel = response.costumeLabel || '';
    }
    participantMessage.value = '参加区分・衣装／メモを保存しました。';
  } catch (reason) {
    participantMessage.value = reason instanceof Error ? reason.message : '参加情報を保存できませんでした。';
  } finally {
    participantSaving.value = false;
  }
}

async function saveProcessRequired() {
  if (!event.value) return;
  participantSaving.value = true; participantMessage.value = '';
  try {
    const response = await portalApi.saveMyEventProcess(event.value.uuid, processRequired.value);
    processRequired.value = response.process;
    if (event.value.membership) event.value.membership.process = response.process;
    participantMessage.value = '加工回し設定を保存しました。';
  } catch (reason) {
    participantMessage.value = reason instanceof Error ? reason.message : '加工回し設定を保存できませんでした。';
  } finally {
    participantSaving.value = false;
  }
}

onMounted(async () => {
  try {
    event.value = (await portalApi.event(String(route.params.uuid))).event;
    participantRole.value = event.value.membership?.participantRole || 'none';
    costumeLabel.value = event.value.membership?.costumeLabel || '';
    processRequired.value = Boolean(event.value.membership?.process);
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
      <a v-if="event.permissions.canManageEvent && event.urls.admin" class="button secondary compact event-admin-link" :href="event.urls.admin">イベント管理画面へ</a>
    </section>

    <section v-if="restriction" :class="['restriction-panel', restriction.tone]">
      <strong>{{ restriction.title }}</strong><p>{{ restriction.body }}</p>
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

      <section v-if="event.participantMemo" class="panel participant-memo-panel">
        <h2>参加者への連絡メモ</h2>
        <p>{{ event.participantMemo }}</p>
      </section>

      <section class="panel action-panel">
        <h2>イベントメニュー</h2>
        <button v-if="event.permissions.canOpenAlbum && event.albumId" class="feature-link album" type="button" @click="openAlbum">
          <span class="feature-icon">📷</span><span><strong>アルバム</strong><small>写真・動画を見る</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canOpenChat && !event.lineOpenchatUrl" type="button" class="feature-link chat" @click="openChat">
          <span class="feature-icon">💬</span><span><strong>チャット</strong><small>参加者とやり取りする</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canViewMembers" class="feature-link members" type="button" @click="router.push({ name: 'event-members', params: { uuid: event.uuid } })">
          <span class="feature-icon">👥</span><span><strong>参加者</strong><small>参加メンバーを見る</small></span><b>›</b>
        </button>
        <button v-if="event.lineOpenchatUrl && event.permissions.canOpenChat" class="feature-link chat" type="button" @click="openOpenchat">
          <span class="feature-icon">💬</span><span><strong>LINEオープンチャット</strong><small>{{ event.lineOpenchatPass ? 'パスコードを確認して参加' : '連絡用チャットを開く' }}</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canViewMembers" class="feature-link members" type="button" @click="router.push({ name: 'event-social', params: { uuid: event.uuid } })">
          <span class="feature-icon">📋</span><span><strong>SNS貼付用</strong><small>Instagram用・X用テキストをコピー</small></span><b>›</b>
        </button>
        <button v-if="event.permissions.canRequestParticipantsPngEmail" class="feature-link members" type="button" :disabled="participantsMailBusy" @click="requestParticipantsEmail">
          <span class="feature-icon">🖼️</span><span><strong>参加者一覧PNG</strong><small>確認済みメールアドレスで受け取る</small></span><b>›</b>
        </button>
        <button v-if="needsPayment" class="feature-link payment" type="button" @click="router.push({name:'event-payment',params:{uuid:event.uuid}})">
          <span class="feature-icon">💳</span><span><strong>お支払い</strong><small>参加費を確認する</small></span><b>›</b>
        </button>
        <button v-if="event.membership?.paymentStatus === 'paid' && event.urls.receipt" class="feature-link payment" type="button" @click="router.push({name:'event-payment',params:{uuid:event.uuid}})">
          <span class="feature-icon">🧾</span><span><strong>支払済みレシート</strong><small>PDFを開く</small></span><b>›</b>
        </button>
        <button v-if="event.tipEnabled && event.membership && !event.membership.isCanceled" class="feature-link tip" type="button" @click="showTipDialog = true">
          <span class="feature-icon">🎁</span><span><strong>投げ銭</strong><small>Squareで主催者を応援する</small></span><b>›</b>
        </button>
        <a v-if="event.permissions.canViewMembers && event.googleFormUrl" class="feature-link survey" :href="event.googleFormUrl" target="_blank" rel="noopener">
          <span class="feature-icon">📝</span><span><strong>アンケート</strong><small>回答フォームを開く</small></span><b>›</b>
        </a>
        <div v-else-if="event.permissions.canViewMembers" class="feature-link survey disabled" aria-disabled="true">
          <span class="feature-icon">📝</span><span><strong>アンケート</strong><small>準備中</small></span><b>—</b>
        </div>
        <div v-if="actionMessage" class="inline-notice">{{ actionMessage }}</div>
        <div v-if="!event.permissions.canOpenAlbum && event.albumId" class="inline-notice">アルバムは参加承認後に閲覧できます。</div>
      </section>

      <section v-if="event.membership" class="panel membership-panel">
        <h2>参加状況</h2>
        <dl class="detail-list">
          <div><dt>承認状態</dt><dd>{{ membershipLabel(event.membership.status, event.membership.isCanceled) }}</dd></div>
          <div><dt>支払状態</dt><dd>{{ event.membership.paymentStatus === 'paid' ? '入金済み' : '未入金' }}</dd></div>
          <div v-if="event.membership.paidAt"><dt>支払日</dt><dd>{{ formatDateTime(event.membership.paidAt) }}</dd></div>
          <div v-if="event.membership.paidAmountYen != null"><dt>実際の支払金額</dt><dd>{{ formatMoney(event.membership.paidAmountYen) }}</dd></div>
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

      <section v-if="event.membership && event.permissions.canEditOwnRole" class="panel participant-settings-panel">
        <h2>本人の参加設定</h2>
        <div class="participant-setting-block">
          <h3>加工回し</h3>
          <label class="toggle-line"><input v-model="processRequired" type="checkbox">加工回しが必要</label>
          <button class="button secondary compact" type="button" :disabled="participantSaving" @click="saveProcessRequired">加工回し設定を保存</button>
        </div>
        <form class="participant-setting-block" @submit.prevent="saveParticipantDetails">
          <h3>参加区分・衣装／その他メモ</h3>
          <label>参加区分<select v-model="participantRole"><option value="none">未設定</option><option value="camera">カメラマン</option><option value="assistant">アシスタント</option><option value="cosplayer">衣装</option><option value="other">その他</option></select></label>
          <label v-if="roleUsesMemo">{{ participantRole === 'cosplayer' ? '衣装名' : 'その他メモ' }}<input v-model="costumeLabel" maxlength="120"></label>
          <button class="button primary compact" :disabled="participantSaving">参加情報を保存</button>
        </form>
        <p v-if="participantMessage" class="inline-notice">{{ participantMessage }}</p>
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
      <input type="hidden" name="portal" value="vue">
      <input type="hidden" name="event_id" :value="event.id">
      <label>金額（円）<input v-model.number="tipAmount" type="number" name="amount_yen" min="100" max="100000" step="1" required></label>
      <div class="tip-presets"><button v-for="amount in [500,1000,3000,5000]" :key="amount" type="button" @click="tipAmount = amount">¥{{ amount.toLocaleString() }}</button></div>
      <div class="modal-actions"><button type="button" class="button secondary" @click="showTipDialog = false">キャンセル</button><button type="submit" class="button primary">投げ銭する</button></div>
    </form>
  </div>
</template>
