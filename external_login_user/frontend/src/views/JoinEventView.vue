<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import { formatDateTime, formatMoney } from '@/utils/format';
const route = useRoute(); const router = useRouter(); const data = ref<any>(null); const error = ref('');
const role = ref('cosplayer'); const costume = ref(''); const process = ref(false); const agreed = ref(false);
const memoEnabled = computed(() => ['cosplayer', 'other'].includes(role.value));
onMounted(async () => { try { const r = await portalApi.joinInfo(String(route.params.uuid)); data.value = r.join; role.value = r.join.participantRole || 'cosplayer'; costume.value = r.join.costumeLabel || ''; process.value = Boolean(r.join.process); } catch (e) { error.value = e instanceof Error ? e.message : '参加申請を読み込めません。'; } });
</script>
<template>
  <button class="back-link" type="button" @click="router.push('/')">← イベント一覧</button><div v-if="error" class="alert error">{{ error }}</div>
  <template v-else-if="data"><section class="page-heading"><div><p class="eyebrow">JOIN EVENT</p><h1>イベント参加</h1><p>{{ data.event.title }}</p></div></section>
    <section class="panel"><dl class="detail-list"><div><dt>開始</dt><dd>{{ formatDateTime(data.event.startsAt) }}</dd></div><div><dt>参加費</dt><dd>{{ formatMoney(data.event.feeYen) }}</dd></div><div><dt>場所</dt><dd>{{ data.event.placeName || '未定' }}</dd></div><div v-if="data.event.address"><dt>住所</dt><dd>{{ data.event.address }}</dd></div></dl></section>
    <div v-if="data.status" :class="['alert', data.status === 'approved' ? 'success' : data.status === 'rejected' ? 'error' : 'warning']">{{ data.status === 'approved' ? 'すでに参加承認済みです。' : data.status === 'pending' ? '参加申請は承認待ちです。' : data.status === 'canceled' ? 'この参加はキャンセル済みです。' : '参加申請は拒否されています。' }}</div>
    <form v-else class="profile-form" method="post" :action="data.submitUrl"><input type="hidden" name="csrf_token" :value="data.csrfToken"><input type="hidden" name="vue" value="1"><section class="panel"><h2>参加区分</h2><div class="role-options"><label v-for="item in [{v:'camera',l:'カメラマン'},{v:'assistant',l:'アシスタント'},{v:'cosplayer',l:'衣装'},{v:'other',l:'その他'}]" :key="item.v"><input v-model="role" type="radio" name="participant_role" :value="item.v">{{ item.l }}</label></div><label>衣装／その他のメモ<input v-model="costume" name="costume_label" maxlength="120" :disabled="!memoEnabled"></label></section><section class="panel"><h2>加工回し</h2><label class="toggle-line"><input v-model="process" type="checkbox" name="process" value="1">加工回しが必要</label></section><section v-if="data.termsRequired" class="panel"><h2>参加規定</h2><label class="toggle-line"><input v-model="agreed" type="checkbox" name="participant_terms_agree" value="1" required><a :href="data.termsUrl" target="_blank" rel="noopener">参加・支払・キャンセル規定</a>に同意する</label></section><div class="form-actions"><button type="button" class="button secondary" @click="router.push('/')">キャンセル</button><button class="button primary" :disabled="data.termsRequired && !agreed">参加を申請する</button></div></form>
  </template>
</template>
