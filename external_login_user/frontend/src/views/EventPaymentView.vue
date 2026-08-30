<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import type { EventItem } from '@/types';
import { formatDateTime, formatMoney } from '@/utils/format';
const route = useRoute(); const router = useRouter(); const event = ref<EventItem | null>(null); const error = ref('');
onMounted(async () => { try { event.value = (await portalApi.event(String(route.params.uuid))).event; } catch (e) { error.value = e instanceof Error ? e.message : '支払情報を取得できません。'; } });
</script>
<template><button class="back-link" type="button" @click="router.push({name:'event',params:{uuid:route.params.uuid}})">← イベント詳細</button><div v-if="error" class="alert error">{{ error }}</div><template v-else-if="event"><section class="page-heading"><div><p class="eyebrow">PAYMENT</p><h1>お支払い</h1><p>{{ event.title }}</p></div></section><section class="panel payment-summary"><dl class="detail-list"><div><dt>参加費</dt><dd>{{ formatMoney(event.feeYen) }}</dd></div><div><dt>支払状態</dt><dd>{{ event.membership?.paymentStatus === 'paid' ? '支払済み' : '未支払' }}</dd></div><div v-if="event.membership?.paidAt"><dt>支払日</dt><dd>{{ formatDateTime(event.membership.paidAt) }}</dd></div><div v-if="event.membership?.paidAmountYen != null"><dt>支払金額</dt><dd>{{ formatMoney(event.membership.paidAmountYen) }}</dd></div><div v-if="event.payUntil"><dt>支払期限</dt><dd>{{ formatDateTime(event.payUntil) }}</dd></div></dl><div class="payment-actions"><a v-if="event.membership?.paymentStatus !== 'paid'" class="button primary wide" :href="event.urls.payment">安全な決済画面へ進む</a><a v-if="event.membership?.paymentStatus === 'paid' && event.urls.receipt" class="button secondary wide" :href="event.urls.receipt" target="_blank" rel="noopener">レシートPDFを開く</a></div><p class="security-note">カード・Apple Pay・Google Payの入力画面は、安全性のため既存のSquare決済画面を使用します。</p></section></template></template>
