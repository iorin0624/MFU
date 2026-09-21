<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'

type Profile = {
  display_name: string; x_handle: string | null; instagram_handle: string | null
  x_handle_visible: boolean; instagram_handle_visible: boolean
  privacy_matrix: PrivacyMatrix
}
type Audience = 'link' | 'logged_in' | 'mutual' | 'private'
type PublicField = 'date' | 'park' | 'costume' | 'memo'
type PrivacyMatrix = Record<Audience, Record<PublicField, boolean>>
const audiences: Array<{ key: Audience; label: string; help: string }> = [
  { key: 'link', label: '共有リンク', help: '共有URLを知っている人' },
  { key: 'logged_in', label: 'ログイン利用者', help: 'INPAにログインしている人' },
  { key: 'mutual', label: '相互登録', help: 'お互いに登録している人' },
  { key: 'private', label: '自分のみ', help: '自分が確認する場合' },
]
const fields: Array<{ key: PublicField; label: string }> = [
  { key: 'date', label: '日付' }, { key: 'park', label: 'パーク' },
  { key: 'costume', label: '服装' }, { key: 'memo', label: 'メモ' },
]
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
  error.value = ''; notice.value = ''
  try {
    const body = { privacy_matrix: profile.value.privacy_matrix }
    profile.value = (await api<{ profile: Profile }>('/privacy-defaults', { method: 'PATCH', body })).profile
    notice.value = '公開設定を保存しました。すべての予定に自動適用されます。'
  } catch (value) { error.value = errorMessage(value) }
}
function allSelected(audience: Audience) {
  return !!profile.value && fields.every(({ key }) => profile.value!.privacy_matrix[audience][key])
}
function toggleAll(audience: Audience) {
  if (!profile.value) return
  const checked = !allSelected(audience)
  fields.forEach(({ key }) => { profile.value!.privacy_matrix[audience][key] = checked })
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
        <h2>予定の公開設定</h2>
        <p class="helper">公開範囲ごとに見せる情報を選択できます。より広い範囲で許可した情報は、それより内側の範囲にも表示されます。</p>
        <div class="privacy-table-wrap">
          <table class="privacy-table">
            <thead><tr><th>公開範囲</th><th>すべて</th><th v-for="field in fields" :key="field.key">{{ field.label }}</th></tr></thead>
            <tbody>
              <tr v-for="audience in audiences" :key="audience.key">
                <th><span>{{ audience.label }}</span><small>{{ audience.help }}</small></th>
                <td><input type="checkbox" :checked="allSelected(audience.key)" :aria-label="`${audience.label}のすべて`" @change="toggleAll(audience.key)"></td>
                <td v-for="field in fields" :key="field.key"><input v-model="profile.privacy_matrix[audience.key][field.key]" type="checkbox" :aria-label="`${audience.label}の${field.label}`"></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="helper">保存すると、登録済みを含むすべての予定に即時適用されます。</p>
        <button class="button">公開設定を保存</button>
      </form>
    </template>
  </section>
</template>
