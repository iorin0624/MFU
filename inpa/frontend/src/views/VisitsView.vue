<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'

type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Visit = {
  public_id: string; season_public_id: string; season_name: string; visit_date: string
  park: string | null; arrival_time: string | null; costume: string | null; memo: string | null
  visibility: string; detail_level: string
}
type Form = Omit<Visit, 'public_id' | 'season_name'>
const blank = (): Form => ({ season_public_id: '', visit_date: '', park: 'undecided', arrival_time: null, costume: null, memo: null, visibility: 'link', detail_level: 'park' })
const seasons = ref<Season[]>([]); const visits = ref<Visit[]>([]); const form = ref<Form>(blank())
const editing = ref<string | null>(null); const error = ref(''); const notice = ref('')
const errorMessage = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

async function load() {
  try {
    const [seasonData, visitData] = await Promise.all([api<{ seasons: Season[] }>('/seasons'), api<{ visits: Visit[] }>('/visits')])
    seasons.value = seasonData.seasons; visits.value = visitData.visits
    if (!form.value.season_public_id && seasons.value[0]) form.value.season_public_id = seasons.value[0].public_id
  } catch (value) { error.value = errorMessage(value) }
}
function edit(visit: Visit) {
  editing.value = visit.public_id
  form.value = { season_public_id: visit.season_public_id, visit_date: visit.visit_date, park: visit.park, arrival_time: visit.arrival_time, costume: visit.costume, memo: visit.memo, visibility: visit.visibility, detail_level: visit.detail_level }
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
function reset() { editing.value = null; form.value = blank(); if (seasons.value[0]) form.value.season_public_id = seasons.value[0].public_id }
async function save() {
  error.value = ''; notice.value = ''
  try {
    const path = editing.value ? `/visits/${editing.value}` : '/visits'
    await api(path, { method: editing.value ? 'PATCH' : 'POST', body: form.value })
    notice.value = editing.value ? '予定を更新しました。' : '予定を追加しました。'
    reset(); await load()
  } catch (value) { error.value = errorMessage(value) }
}
async function remove(visit: Visit) {
  if (!confirm(`${visit.visit_date}の予定を削除しますか？`)) return
  try { await api(`/visits/${visit.public_id}`, { method: 'DELETE' }); await load(); notice.value = '予定を削除しました。' }
  catch (value) { error.value = errorMessage(value) }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>予定</h1><p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="!seasons.length" class="notice">現在、選択できるシーズンがありません。</p>
    <form v-else class="form-stack" @submit.prevent="save">
      <h2>{{ editing ? '予定を編集' : '予定を追加' }}</h2>
      <label class="field">シーズン<select v-model="form.season_public_id" required><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ season.start_date }}〜{{ season.end_date }}）</option></select></label>
      <label class="field">日付<input v-model="form.visit_date" type="date" required></label>
      <label class="field">パーク<select v-model="form.park"><option value="undecided">未定</option><option value="land">ランド</option><option value="sea">シー</option><option value="both">両方</option></select></label>
      <label class="field">到着時刻<input v-model="form.arrival_time" type="time"></label>
      <label class="field">服装<input v-model="form.costume" maxlength="100"></label>
      <label class="field">メモ<textarea v-model="form.memo" maxlength="500" rows="4"></textarea></label>
      <label class="field">公開対象<select v-model="form.visibility"><option value="link">共有リンク</option><option value="logged_in">ログイン利用者</option><option value="following">登録済み</option><option value="mutual">相互登録</option><option value="private">自分のみ</option></select></label>
      <label class="field">公開情報<select v-model="form.detail_level"><option value="date">日付のみ</option><option value="park">日付・パーク</option><option value="memo">日付・パーク・メモ</option><option value="full">すべて</option></select></label>
      <div class="actions"><button class="button">{{ editing ? '更新' : '追加' }}</button><button v-if="editing" type="button" class="button secondary" @click="reset">キャンセル</button></div>
    </form>
    <div class="visit-list"><article v-for="visit in visits" :key="visit.public_id" class="visit-card"><h3>{{ visit.visit_date }} / {{ visit.park ?? '未定' }}</h3><p>{{ visit.season_name }}</p><p v-if="visit.arrival_time">到着 {{ visit.arrival_time }}</p><p v-if="visit.costume">服装 {{ visit.costume }}</p><p v-if="visit.memo">{{ visit.memo }}</p><div class="actions"><button class="button secondary" @click="edit(visit)">編集</button><button class="button danger" @click="remove(visit)">削除</button></div></article></div>
  </section>
</template>
