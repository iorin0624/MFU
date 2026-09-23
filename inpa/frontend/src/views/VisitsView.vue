<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'
import { parkLabel } from '@/lib/park'

const route = useRoute(); const router = useRouter()
type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Visit = {
  public_id: string; season_public_id: string; season_name: string; visit_date: string
  park: string | null; arrival_time: string | null; costume: string | null; memo: string | null
}
type Form = Omit<Visit, 'public_id' | 'season_name'>
type Restriction = { public_id: string; name: string; description: string | null; park_scope: string; start_date: string; end_date: string }
const blank = (): Form => ({ season_public_id: '', visit_date: '', park: 'undecided', arrival_time: null, costume: null, memo: null })
const seasons = ref<Season[]>([]); const visits = ref<Visit[]>([]); const form = ref<Form>(blank())
const editing = ref<string | null>(null); const error = ref(''); const notice = ref('')
const warnings = ref<Restriction[]>([])
const errorMessage = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'
const selectedSeason = computed(() => seasons.value.find(season => season.public_id === form.value.season_public_id))
const dateIsInSeason = computed(() => {
  const season = selectedSeason.value
  return !!season && !!form.value.visit_date
    && form.value.visit_date >= season.start_date && form.value.visit_date <= season.end_date
})

async function load() {
  try {
    const [seasonData, visitData] = await Promise.all([api<{ seasons: Season[] }>('/seasons'), api<{ visits: Visit[] }>('/visits')])
    seasons.value = seasonData.seasons; visits.value = visitData.visits
    if (!seasons.value.some(season => season.public_id === form.value.season_public_id) && seasons.value[0]) {
      const matching = seasons.value.find(season => form.value.visit_date >= season.start_date && form.value.visit_date <= season.end_date)
      form.value.season_public_id = (matching ?? seasons.value[0]).public_id
    }
  } catch (value) { error.value = errorMessage(value) }
}
async function loadWarnings() {
  if (!form.value.season_public_id || !form.value.visit_date) { warnings.value = []; return }
  const query = new URLSearchParams({ season_id: form.value.season_public_id, date: form.value.visit_date, park: form.value.park ?? '' })
  try { warnings.value = (await api<{ restrictions: Restriction[] }>(`/restrictions?${query}`)).restrictions }
  catch { warnings.value = [] }
}
function edit(visit: Visit) {
  editing.value = visit.public_id
  form.value = { season_public_id: visit.season_public_id, visit_date: visit.visit_date, park: visit.park, arrival_time: visit.arrival_time, costume: visit.costume, memo: visit.memo }
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
function reset() { editing.value = null; form.value = blank(); if (seasons.value[0]) form.value.season_public_id = seasons.value[0].public_id }
async function save() {
  error.value = ''; notice.value = ''
  if (!dateIsInSeason.value) {
    error.value = '選択したシーズンの期間内の日付を指定してください。'
    return
  }
  try {
    const savedDate = form.value.visit_date
    const path = editing.value ? `/visits/${editing.value}` : '/visits'
    await api(path, { method: editing.value ? 'PATCH' : 'POST', body: form.value })
    notice.value = editing.value ? '予定を更新しました。' : '予定を追加しました。'
    if (!editing.value && route.query.return === 'calendar') {
      await router.push({ path: '/', query: { date: savedDate, season: form.value.season_public_id } }); return
    }
    reset(); await load()
  } catch (value) { error.value = errorMessage(value) }
}
async function remove(visit: Visit) {
  if (!confirm(`${formatJapaneseDate(visit.visit_date)}の予定を削除しますか？`)) return
  try { await api(`/visits/${visit.public_id}`, { method: 'DELETE' }); await load(); notice.value = '予定を削除しました。' }
  catch (value) { error.value = errorMessage(value) }
}
onMounted(async () => {
  if (typeof route.query.season === 'string') form.value.season_public_id = route.query.season
  if (typeof route.query.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(route.query.date)) {
    form.value.visit_date = route.query.date
  }
  await load()
})
watch(() => [form.value.season_public_id, form.value.visit_date, form.value.park], loadWarnings)
watch(() => form.value.season_public_id, () => {
  const season = selectedSeason.value
  if (season && form.value.visit_date
    && (form.value.visit_date < season.start_date || form.value.visit_date > season.end_date)) {
    form.value.visit_date = ''
  }
})
</script>

<template>
  <section class="panel">
    <h1>予定</h1><p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="!seasons.length" class="notice">現在、選択できるシーズンがありません。</p>
    <form v-else class="form-stack" @submit.prevent="save">
      <h2>{{ editing ? '予定を編集' : '予定を追加' }}</h2>
      <label class="field">シーズン<select v-model="form.season_public_id" required><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option></select></label>
      <label class="field">日付<input v-model="form.visit_date" type="date" :min="selectedSeason?.start_date" :max="selectedSeason?.end_date" required><small v-if="form.visit_date">{{ formatJapaneseDate(form.visit_date) }}</small><small v-if="selectedSeason">登録可能期間：{{ formatJapaneseDate(selectedSeason.start_date) }}〜{{ formatJapaneseDate(selectedSeason.end_date) }}</small></label>
      <label class="field">パーク<select v-model="form.park"><option value="undecided">未定</option><option value="land">🏰TDL</option><option value="sea">🌍TDS</option><option value="both">🏰TDL・🌍TDS</option></select></label>
      <div v-for="warning in warnings" :key="warning.public_id" class="warning-notice"><strong>{{ warning.name }}</strong><p>{{ warning.description || 'この日は対象期間です。内容を確認してください。' }}</p><small>{{ formatJapaneseDate(warning.start_date) }}〜{{ formatJapaneseDate(warning.end_date) }}</small></div>
      <label class="field">到着時刻<input v-model="form.arrival_time" type="time"></label>
      <label class="field">服装<input v-model="form.costume" maxlength="100"></label>
      <label class="field">メモ<textarea v-model="form.memo" maxlength="500" rows="4"></textarea></label>
      <p class="helper">公開する範囲と情報は、プロフィールで選択したシーズンの公開設定が適用されます。</p>
      <div class="actions"><button class="button" :disabled="!dateIsInSeason">{{ editing ? '更新' : '追加' }}</button><button v-if="editing" type="button" class="button secondary" @click="reset">キャンセル</button></div>
    </form>
    <div class="visit-list"><article v-for="visit in visits" :key="visit.public_id" class="visit-card"><h3>{{ formatJapaneseDate(visit.visit_date) }} / {{ parkLabel(visit.park) }}</h3><p>{{ visit.season_name }}</p><p v-if="visit.arrival_time">到着 {{ visit.arrival_time }}</p><p v-if="visit.costume">服装 {{ visit.costume }}</p><p v-if="visit.memo">{{ visit.memo }}</p><div class="actions"><button class="button secondary" @click="edit(visit)">編集</button><button class="button danger" @click="remove(visit)">削除</button></div></article></div>
  </section>
</template>
