<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'

type Step = 'privacy' | 'visit' | 'completed'
type Audience = 'link' | 'logged_in' | 'mutual' | 'private'
type PublicField = 'date' | 'park' | 'costume' | 'memo'
type PrivacyMatrix = Record<Audience, Record<PublicField, boolean>>
type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Restriction = { public_id: string; name: string; description: string | null; start_date: string; end_date: string }
const audiences: Array<{ key: Audience; label: string; help: string }> = [
  { key: 'link', label: '共有リンク', help: '共有URLを知っている人' },
  { key: 'logged_in', label: 'ログイン利用者', help: 'INPAの利用者' },
  { key: 'mutual', label: '相互登録', help: 'お互いにフォロー中' },
  { key: 'private', label: '自分のみ', help: '自分が確認する場合' },
]
const fields: Array<{ key: PublicField; label: string }> = [
  { key: 'date', label: '日付' }, { key: 'park', label: 'パーク' },
  { key: 'costume', label: '服装' }, { key: 'memo', label: 'メモ' },
]
const router = useRouter()
const step = ref<Step>('privacy'); const loading = ref(true); const pending = ref(false)
const seasons = ref<Season[]>([]); const seasonId = ref(''); const privacyMatrix = ref<PrivacyMatrix | null>(null)
const visitDate = ref(''); const park = ref('undecided'); const arrivalTime = ref(''); const costume = ref(''); const memo = ref('')
const warnings = ref<Restriction[]>([]); const error = ref('')
const activeSeason = computed(() => seasons.value.find(season => season.public_id === seasonId.value))
const dateIsValid = computed(() => !!activeSeason.value && !!visitDate.value && visitDate.value >= activeSeason.value.start_date && visitDate.value <= activeSeason.value.end_date)
const message = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

function allSelected(audience: Audience) { return !!privacyMatrix.value && fields.every(({ key }) => privacyMatrix.value![audience][key]) }
function toggleAll(audience: Audience) {
  if (!privacyMatrix.value) return
  const checked = !allSelected(audience); fields.forEach(({ key }) => { privacyMatrix.value![audience][key] = checked })
}
async function loadPrivacy() {
  if (!seasonId.value) { privacyMatrix.value = null; return }
  try {
    const query = new URLSearchParams({ season_id: seasonId.value })
    privacyMatrix.value = (await api<{ privacy_matrix: PrivacyMatrix }>(`/privacy-defaults?${query}`)).privacy_matrix
  } catch (cause) { error.value = message(cause) }
}
async function savePrivacy() {
  pending.value = true; error.value = ''
  try {
    if (seasonId.value && privacyMatrix.value) {
      await api('/privacy-defaults', { method: 'PATCH', body: { season_public_id: seasonId.value, privacy_matrix: privacyMatrix.value } })
    }
    await api('/onboarding', { method: 'PATCH', body: { step: 'visit' } })
    step.value = 'visit'
  } catch (cause) { error.value = message(cause) } finally { pending.value = false }
}
async function loadWarnings() {
  if (!seasonId.value || !visitDate.value) { warnings.value = []; return }
  const query = new URLSearchParams({ season_id: seasonId.value, date: visitDate.value, park: park.value })
  try { warnings.value = (await api<{ restrictions: Restriction[] }>(`/restrictions?${query}`)).restrictions }
  catch { warnings.value = [] }
}
async function saveVisit() {
  if (!dateIsValid.value) return
  pending.value = true; error.value = ''
  try {
    await api('/visits', { method: 'POST', body: {
      season_public_id: seasonId.value, visit_date: visitDate.value, park: park.value,
      arrival_time: arrivalTime.value || null, costume: costume.value || null, memo: memo.value || null,
    } })
    await router.push({ path: '/', query: { season: seasonId.value, date: visitDate.value } })
  } catch (cause) { error.value = message(cause) } finally { pending.value = false }
}
async function skipVisit() {
  pending.value = true; error.value = ''
  try { await api('/onboarding', { method: 'PATCH', body: { step: 'completed' } }); await router.push('/') }
  catch (cause) { error.value = message(cause) } finally { pending.value = false }
}
onMounted(async () => {
  try {
    const status = await api<{ step: Step }>('/onboarding')
    if (status.step === 'completed') { await router.replace('/'); return }
    step.value = status.step
    seasons.value = (await api<{ seasons: Season[] }>('/seasons')).seasons
    const today = new Date().toLocaleDateString('sv-SE')
    const season = seasons.value.find(item => today >= item.start_date && today <= item.end_date) ?? seasons.value[0]
    seasonId.value = season?.public_id ?? ''; visitDate.value = season && today >= season.start_date && today <= season.end_date ? today : season?.start_date ?? ''
    if (step.value === 'privacy') await loadPrivacy()
    if (step.value === 'visit') await loadWarnings()
  } catch (cause) { error.value = message(cause) } finally { loading.value = false }
})
watch(() => [seasonId.value, visitDate.value, park.value], loadWarnings)
watch(seasonId, async () => {
  const season = activeSeason.value
  if (season && (!visitDate.value || visitDate.value < season.start_date || visitDate.value > season.end_date)) visitDate.value = season.start_date
  if (step.value === 'privacy') await loadPrivacy()
})
</script>

<template>
  <section class="panel">
    <p v-if="step === 'privacy'" class="wizard-progress">初回設定 2 / 3</p><p v-else class="wizard-progress">初回設定 3 / 3</p>
    <p v-if="loading" class="helper">読み込み中…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="!loading && step === 'privacy'">
      <h1>公開範囲設定</h1><p>予定をどこまで公開するか選択します。この設定は後から変更できます。</p>
      <form class="form-stack" @submit.prevent="savePrivacy">
        <label v-if="seasons.length" class="field">設定するシーズン<select v-model="seasonId"><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option></select></label>
        <p v-else class="notice">現在、設定できるシーズンがありません。次へ進めます。</p>
        <div v-if="privacyMatrix" class="privacy-table-wrap"><table class="privacy-table"><thead><tr><th>公開範囲</th><th>すべて</th><th v-for="field in fields" :key="field.key">{{ field.label }}</th></tr></thead><tbody><tr v-for="audience in audiences" :key="audience.key"><th><span>{{ audience.label }}</span><small>{{ audience.help }}</small></th><td><input type="checkbox" :checked="allSelected(audience.key)" :aria-label="`${audience.label}のすべて`" @change="toggleAll(audience.key)"></td><td v-for="field in fields" :key="field.key"><input v-model="privacyMatrix[audience.key][field.key]" type="checkbox" :aria-label="`${audience.label}の${field.label}`"></td></tr></tbody></table></div>
        <button class="button" :disabled="pending || (seasons.length > 0 && !privacyMatrix)">{{ pending ? '保存中…' : '保存して次へ' }}</button>
      </form>
    </template>
    <template v-if="!loading && step === 'visit'">
      <h1>最初の予定追加</h1><p>最初のインパーク予定を登録します。予定が未定の場合は、今は追加せずに開始できます。</p>
      <form v-if="seasons.length" class="form-stack" @submit.prevent="saveVisit">
        <label class="field">シーズン<select v-model="seasonId"><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option></select></label>
        <label class="field">日付<input v-model="visitDate" type="date" :min="activeSeason?.start_date" :max="activeSeason?.end_date" required></label>
        <label class="field">パーク<select v-model="park"><option value="undecided">未定</option><option value="land">🏰TDL</option><option value="sea">🌍TDS</option><option value="both">🏰TDL・🌍TDS</option></select></label>
        <div v-for="warning in warnings" :key="warning.public_id" class="warning-notice"><strong>{{ warning.name }}</strong><p>{{ warning.description || 'この日は対象期間です。内容を確認してください。' }}</p></div>
        <label class="field">到着時刻<input v-model="arrivalTime" type="time"></label><label class="field">服装<input v-model="costume" maxlength="100"></label><label class="field">メモ<textarea v-model="memo" maxlength="500" rows="4"></textarea></label>
        <div class="wizard-actions"><button class="button" :disabled="pending || !dateIsValid">予定を追加して開始</button><button class="button secondary" type="button" :disabled="pending" @click="skipVisit">今は予定を追加しない</button></div>
      </form>
      <div v-else><p class="notice">現在、予定を追加できるシーズンがありません。</p><button class="button" type="button" :disabled="pending" @click="skipVisit">INPAを開始</button></div>
    </template>
  </section>
</template>
