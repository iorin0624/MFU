<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import type { EventItem } from '@/types';
import { formatDateTime, formatMoney } from '@/utils/format';
import { eventThemeStyle, useDocumentEventTheme } from '@/utils/eventTheme';

type Bank={id:number;label:string;bankName:string;branchName:string;accountKind:string;accountNumber:string;accountHolder:string;memo?:string};
type PaymentOptions={methods:{card:boolean;paypay:boolean;bank:boolean};feeYen:number;paypayUrl?:string;paypayDisplay?:string;banks:Bank[];squareUrl:string};
const route=useRoute(); const router=useRouter();
const event=ref<EventItem|null>(null); const options=ref<PaymentOptions|null>(null);
useDocumentEventTheme(computed(() => event.value?.themeColor));
const error=ref(''); const busy=ref(false); const method=ref<'card'|'paypay'|'bank'>('card');
const remitterName=ref(''); const bankId=ref(''); const depositDate=ref(new Date().toISOString().slice(0,10));
const availableMethods=computed(()=>options.value ? (['card','paypay','bank'] as const).filter(key=>options.value?.methods[key]) : []);
function methodLabel(value:string){return value==='card'?'クレジットカード・Apple Pay・Google Pay':value==='paypay'?'PayPay送金':'銀行振込';}
async function load(){try{const uuid=String(route.params.uuid);const [eventResponse,paymentResponse]=await Promise.all([portalApi.event(uuid),portalApi.paymentOptions(uuid)]);event.value=eventResponse.event;options.value=paymentResponse.payment as PaymentOptions;method.value=availableMethods.value[0]||'card';bankId.value=String(options.value.banks[0]?.id||'');}catch(reason){error.value=reason instanceof Error?reason.message:'支払情報を取得できません。';}}
async function submit(){if(!options.value||busy.value)return;if(method.value==='card'){window.location.assign(options.value.squareUrl);return;}const form=new FormData();form.append('remitter_name',remitterName.value);if(method.value==='bank'){form.append('bank_id',bankId.value);form.append('deposit_date',depositDate.value);}busy.value=true;error.value='';try{if(method.value==='paypay')await portalApi.submitPayPay(String(route.params.uuid),form);else await portalApi.submitBankPayment(String(route.params.uuid),form);await router.replace({name:'event',params:{uuid:route.params.uuid},query:{payment:'submitted'}});}catch(reason){error.value=reason instanceof Error?reason.message:'申告を保存できませんでした。';}finally{busy.value=false;}}
onMounted(load);
</script>

<template>
 <div class="event-theme" :style="eventThemeStyle(event?.themeColor)">
  <button class="back-link" type="button" @click="router.push({name:'event',params:{uuid:route.params.uuid}})">← イベント詳細</button>
  <div v-if="error" class="alert error">{{ error }}</div>
  <template v-if="event">
    <section class="page-heading"><div><p class="eyebrow">PAYMENT</p><h1>お支払い</h1><p>{{ event.title }}</p></div></section>
    <section class="panel payment-summary"><dl class="detail-list"><div><dt>参加費</dt><dd>{{ formatMoney(options?.feeYen ?? event.feeYen) }}</dd></div><div><dt>支払状態</dt><dd>{{ event.membership?.paymentStatus === 'paid' ? '支払済み' : event.membership?.paymentStatus === 'pending' ? '確認中' : '未支払' }}</dd></div><div v-if="event.membership?.paidAt"><dt>支払日</dt><dd>{{ formatDateTime(event.membership.paidAt) }}</dd></div><div v-if="event.membership?.paidAmountYen != null"><dt>支払金額</dt><dd>{{ formatMoney(event.membership.paidAmountYen) }}</dd></div><div v-if="event.payUntil"><dt>支払期限</dt><dd>{{ formatDateTime(event.payUntil) }}</dd></div></dl><a v-if="event.membership?.paymentStatus === 'paid' && event.urls.receipt" class="button secondary wide" :href="event.urls.receipt" target="_blank" rel="noopener">レシートPDFを開く</a></section>
    <section v-if="event.membership?.paymentStatus !== 'paid' && options" class="panel payment-method-panel"><h2>支払方法</h2><label v-for="item in availableMethods" :key="item" class="payment-method-option"><input v-model="method" type="radio" :value="item"><span>{{ methodLabel(item) }}</span></label>
      <div v-if="method==='card'" class="alert info"><strong>Squareの安全な決済画面を使用します</strong><span>決済完了後、このイベント管理画面へ自動的に戻ります。</span></div>
      <div v-else-if="method==='paypay'" class="payment-form"><a v-if="options.paypayUrl" class="button secondary wide" :href="options.paypayUrl" target="_blank" rel="noopener">PayPayを開く</a><p v-if="options.paypayDisplay">{{ options.paypayDisplay }}</p><label>送金名<input v-model="remitterName" autocomplete="name" required></label><small>PayPayで表示される送金名を入力してください。</small></div>
      <div v-else class="payment-form"><label>振込先<select v-model="bankId" required><option v-for="bank in options.banks" :key="bank.id" :value="String(bank.id)">{{ bank.label }}（{{ bank.bankName }} {{ bank.branchName }}）</option></select></label><template v-for="bank in options.banks" :key="bank.id"><dl v-if="String(bank.id)===bankId" class="detail-list bank-account"><div><dt>金融機関</dt><dd>{{ bank.bankName }}</dd></div><div><dt>支店</dt><dd>{{ bank.branchName }}</dd></div><div><dt>口座</dt><dd>{{ bank.accountKind }} {{ bank.accountNumber }}</dd></div><div><dt>名義</dt><dd>{{ bank.accountHolder }}</dd></div><div v-if="bank.memo"><dt>備考</dt><dd>{{ bank.memo }}</dd></div></dl></template><label>振込元名<input v-model="remitterName" autocomplete="name" required></label><label>着金日<input v-model="depositDate" type="date" required></label></div>
      <button type="button" class="button primary wide" :disabled="busy || (method!=='card'&&!remitterName.trim()) || (method==='bank'&&(!bankId||!depositDate))" @click="submit">{{ busy?'処理中…':method==='card'?'Square決済へ進む':'申告を送信' }}</button>
    </section>
  </template>
 </div>
</template>
