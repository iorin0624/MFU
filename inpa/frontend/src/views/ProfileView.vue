<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'

type Profile = {
  display_name: string; x_handle: string | null; instagram_handle: string | null
  x_handle_visible: boolean; instagram_handle_visible: boolean
  default_visibility: string; default_detail_level: string
}
const profile = ref<Profile | null>(null)
const error = ref(''); const notice = ref('')
const errorMessage = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

async function load() {
  try { profile.value = (await api<{ profile: Profile }>('/profile')).profile }
  catch (value) { error.value = errorMessage(value) }
}
async function saveProfile() {
  if (!profile.value) return
  error.value = ''; notice.value = ''
  try {
    const body = {
      display_name: profile.value.display_name, x_handle: profile.value.x_handle,
      instagram_handle: profile.value.instagram_handle,
      x_handle_visible: profile.value.x_handle_visible,
      instagram_handle_visible: profile.value.instagram_handle_visible,
    }
    profile.value = (await api<{ profile: Profile }>('/profile', { method: 'PATCH', body })).profile
    notice.value = 'プロフィールを保存しました。'
  } catch (value) { error.value = errorMessage(value) }
}
async function savePrivacy() {
  if (!profile.value) return
  try {
    const body = { default_visibility: profile.value.default_visibility, default_detail_level: profile.value.default_detail_level }
    profile.value = (await api<{ profile: Profile }>('/privacy-defaults', { method: 'PATCH', body })).profile
    notice.value = '標準の公開設定を保存しました。'
  } catch (value) { error.value = errorMessage(value) }
}
async function applyDefaults() {
  if (!confirm('すべての登録済み予定を現在の標準公開設定に変更します。よろしいですか？')) return
  try {
    const result = await api<{ updated_count: number }>('/privacy-defaults/apply-to-visits', { method: 'POST', body: { confirmed: true } })
    notice.value = `${result.updated_count}件の予定に反映しました。`
  } catch (value) { error.value = errorMessage(value) }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>プロフィール</h1>
    <p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <template v-if="profile">
      <form class="form-stack" @submit.prevent="saveProfile">
        <label class="field">表示名<input v-model="profile.display_name" required maxlength="40"></label>
        <label class="field">X<input v-model="profile.x_handle" maxlength="15" placeholder="@なしで入力"></label>
        <label class="field-check"><input v-model="profile.x_handle_visible" type="checkbox">共有画面にXを表示する</label>
        <label class="field">Instagram<input v-model="profile.instagram_handle" maxlength="30" placeholder="@なしで入力"></label>
        <label class="field-check"><input v-model="profile.instagram_handle_visible" type="checkbox">共有画面にInstagramを表示する</label>
        <button class="button">プロフィールを保存</button>
      </form>
      <hr>
      <form class="form-stack" @submit.prevent="savePrivacy">
        <h2>予定の標準公開設定</h2>
        <label class="field">公開対象<select v-model="profile.default_visibility"><option value="link">共有リンク</option><option value="logged_in">ログイン利用者</option><option value="following">登録済み</option><option value="mutual">相互登録</option><option value="private">自分のみ</option></select></label>
        <label class="field">公開情報<select v-model="profile.default_detail_level"><option value="date">日付のみ</option><option value="park">日付・パーク</option><option value="memo">日付・パーク・メモ</option><option value="full">すべて</option></select></label>
        <div class="actions"><button class="button">標準設定を保存</button><button type="button" class="button secondary" @click="applyDefaults">既存予定にも反映</button></div>
      </form>
    </template>
  </section>
</template>
