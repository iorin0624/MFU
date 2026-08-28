<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { ParticipantPass } from '@/types';
import { formatDateTime, formatMoney } from '@/utils/format';

const route = useRoute();
const router = useRouter();
const participantPass = ref<ParticipantPass | null>(null);
const loading = ref(true);
const error = ref('');
const now = ref(new Date());
let timer = 0;

const currentTime = computed(() => new Intl.DateTimeFormat('ja-JP', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
}).format(now.value));

onMounted(async () => {
  timer = window.setInterval(() => { now.value = new Date(); }, 1000);
  try {
    participantPass.value = (await portalApi.participantPass(String(route.params.uuid))).participantPass;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '参加証を取得できませんでした。';
  } finally {
    loading.value = false;
  }
});

onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <button type="button" class="back-link pass-back" @click="router.push({ name: 'event', params: { uuid: route.params.uuid } })">← イベント詳細</button>
  <LoadingBlock v-if="loading">参加証を読み込んでいます</LoadingBlock>
  <div v-else-if="error" class="alert error">
    <strong>参加証を表示できません</strong>
    <span>{{ error }}</span>
  </div>
  <section v-else-if="participantPass" class="participant-pass-stage" aria-label="参加証">
    <div class="pass-watermark" aria-hidden="true">
      MFU PASS<br>{{ participantPass.event.title }}<br>{{ participantPass.participant.nickname }}<br>{{ currentTime }}
    </div>
    <div class="pass-float" aria-hidden="true">
      <span>📸</span><span>✨</span><span>🎉</span><span>🪄</span><span>⭐</span><span>🎈</span>
    </div>
    <article class="participant-pass-card">
      <p class="pass-eyebrow">MFU EVENT PASS</p>
      <img v-if="participantPass.participant.avatarUrl" :src="participantPass.participant.avatarUrl" alt="" class="pass-avatar" referrerpolicy="no-referrer">
      <div v-else class="pass-avatar pass-avatar-placeholder" aria-hidden="true">👤</div>

      <section class="pass-identity">
        <span>ニックネーム</span>
        <strong>{{ participantPass.participant.nickname }}</strong>
      </section>

      <section class="pass-event-meta">
        <span>会場名と開催日</span>
        <strong>{{ participantPass.event.placeName || '会場未設定' }}</strong>
        <time>{{ participantPass.event.startsAt ? formatDateTime(participantPass.event.startsAt) : '日時未設定' }}</time>
      </section>

      <div class="pass-status-grid">
        <section>
          <span>支払状態</span>
          <strong :class="['pass-pill', `payment-${participantPass.payment.key}`]">{{ participantPass.payment.label }}</strong>
          <small v-if="participantPass.payment.key === 'paid' && participantPass.payment.amountYen != null">{{ formatMoney(participantPass.payment.amountYen) }}</small>
        </section>
        <section>
          <span>受付ステータス</span>
          <strong :class="['pass-pill', participantPass.checkin.checkedIn ? 'checkin-done' : 'checkin-waiting']">
            {{ participantPass.checkin.checkedIn ? '受付済み' : '未受付' }}
          </strong>
        </section>
      </div>

      <dl v-if="participantPass.checkin.checkedIn" class="pass-checkin-detail">
        <div><dt>受付日時</dt><dd>{{ formatDateTime(participantPass.checkin.at) }}</dd></div>
        <div><dt>受付方法</dt><dd>{{ participantPass.checkin.methodLabel || '—' }}</dd></div>
      </dl>
      <div v-else class="pass-checkin-guide">
        <strong>会場での受付が必要です</strong>
        <span>会場に掲示されたQRコードから受付してください。</span>
      </div>

      <time class="pass-current-time">現在時刻：{{ currentTime }}</time>
      <p class="pass-hint">提示中は画面の明るさを上げ、受付担当者にこの画面をお見せください。</p>
    </article>
  </section>
</template>
