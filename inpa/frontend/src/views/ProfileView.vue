<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate, formatJapaneseDateTime } from '@/lib/date'
import { createPasskey, passkeysSupported } from '@/lib/webauthn'

type Profile = {
  display_name: string; x_handle: string | null; instagram_handle: string | null
  x_handle_visible: boolean; instagram_handle_visible: boolean
  privacy_matrix: PrivacyMatrix
}
type Audience = 'link' | 'logged_in' | 'mutual' | 'private'
type PublicField = 'date' | 'park' | 'costume' | 'memo'
type PrivacyMatrix = Record<Audience, Record<PublicField, boolean>>
type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Passkey = {
  public_id: string; name: string; created_at: string; last_used_at: string | null
  device_type: string | null; backed_up: boolean
}
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
const seasons = ref<Season[]>([]); const selectedSeasonId = ref('')
const privacyMatrix = ref<PrivacyMatrix | null>(null); const customized = ref(false)
const error = ref(''); const notice = ref('')
const passkeys = ref<Passkey[]>([]); const currentPassword = ref(''); const newPassword = ref('')
const newPasswordAgain = ref(''); const passkeyPassword = ref(''); const passkeyName = ref('')
const securityPending = ref(false)
const errorMessage = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

async function load() {
  try {
    const [profileData, seasonData, passkeyData] = await Promise.all([
      api<{ profile: Profile }>('/profile'), api<{ seasons: Season[] }>('/seasons'),
      api<{ passkeys: Passkey[] }>('/auth/passkeys'),
    ])
    profile.value = profileData.profile; seasons.value = seasonData.seasons; passkeys.value = passkeyData.passkeys
    const today = new Date().toLocaleDateString('sv-SE')
    selectedSeasonId.value = (seasons.value.find(season => today >= season.start_date && today <= season.end_date) ?? seasons.value[0])?.public_id ?? ''
    await loadPrivacy()
  }
  catch (value) { error.value = errorMessage(value) }
}
async function loadPrivacy() {
  if (!selectedSeasonId.value) { privacyMatrix.value = null; return }
  error.value = ''; notice.value = ''
  try {
    const query = new URLSearchParams({ season_id: selectedSeasonId.value })
    const result = await api<{ privacy_matrix: PrivacyMatrix; customized: boolean }>(`/privacy-defaults?${query}`)
    privacyMatrix.value = result.privacy_matrix; customized.value = result.customized
  } catch (value) { error.value = errorMessage(value) }
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
  if (!privacyMatrix.value || !selectedSeasonId.value) return
  error.value = ''; notice.value = ''
  try {
    const body = { season_public_id: selectedSeasonId.value, privacy_matrix: privacyMatrix.value }
    const result = await api<{ privacy_matrix: PrivacyMatrix }>('/privacy-defaults', { method: 'PATCH', body })
    privacyMatrix.value = result.privacy_matrix; customized.value = true
    notice.value = '選択したシーズンの公開設定を保存しました。'
  } catch (value) { error.value = errorMessage(value) }
}
function allSelected(audience: Audience) {
  return !!privacyMatrix.value && fields.every(({ key }) => privacyMatrix.value![audience][key])
}
function toggleAll(audience: Audience) {
  if (!privacyMatrix.value) return
  const checked = !allSelected(audience)
  fields.forEach(({ key }) => { privacyMatrix.value![audience][key] = checked })
}
async function resetPrivacy() {
  if (!selectedSeasonId.value || !confirm('このシーズンの個別設定を解除し、標準設定に戻しますか？')) return
  error.value = ''; notice.value = ''
  try {
    const result = await api<{ privacy_matrix: PrivacyMatrix; customized: boolean }>(`/privacy-defaults/${selectedSeasonId.value}`, { method: 'DELETE' })
    privacyMatrix.value = result.privacy_matrix; customized.value = result.customized
    notice.value = '標準の公開設定に戻しました。'
  } catch (value) { error.value = errorMessage(value) }
}
async function changePassword() {
  error.value = ''; notice.value = ''
  if (newPassword.value !== newPasswordAgain.value) { error.value = '新しいパスワードが一致しません。'; return }
  securityPending.value = true
  try {
    await api('/auth/password', { method: 'PATCH', body: { current_password: currentPassword.value, new_password: newPassword.value } })
    window.location.assign('/login?password_changed=1')
  } catch (value) { error.value = errorMessage(value) }
  finally { securityPending.value = false }
}
async function addPasskey() {
  error.value = ''; notice.value = ''
  if (!passkeysSupported()) { error.value = 'このブラウザではパスキーを利用できません。'; return }
  if (!passkeyPassword.value || !passkeyName.value.trim()) { error.value = '確認用パスワードとパスキー名を入力してください。'; return }
  securityPending.value = true
  try {
    const start = await api<{ challenge_id: string; options: Parameters<typeof createPasskey>[0] }>('/auth/passkeys/registration/options', { method: 'POST', body: { password: passkeyPassword.value } })
    const credential = await createPasskey(start.options)
    await api('/auth/passkeys/registration/complete', { method: 'POST', body: { challenge_id: start.challenge_id, credential, name: passkeyName.value.trim() } })
    passkeys.value = (await api<{ passkeys: Passkey[] }>('/auth/passkeys')).passkeys
    passkeyPassword.value = ''; passkeyName.value = ''; notice.value = 'パスキーを追加しました。'
  } catch (value) { error.value = errorMessage(value) }
  finally { securityPending.value = false }
}
async function savePasskeyName(passkey: Passkey) {
  error.value = ''; notice.value = ''
  try {
    await api(`/auth/passkeys/${passkey.public_id}`, { method: 'PATCH', body: { name: passkey.name } })
    notice.value = 'パスキー名を保存しました。'
  } catch (value) { error.value = errorMessage(value) }
}
async function deletePasskey(passkey: Passkey) {
  if (!passkeyPassword.value) { error.value = 'パスキーを削除するには確認用パスワードを入力してください。'; return }
  if (!confirm(`「${passkey.name}」を削除しますか？`)) return
  securityPending.value = true; error.value = ''; notice.value = ''
  try {
    await api(`/auth/passkeys/${passkey.public_id}`, { method: 'DELETE', body: { password: passkeyPassword.value } })
    passkeys.value = passkeys.value.filter(item => item.public_id !== passkey.public_id)
    passkeyPassword.value = ''; notice.value = 'パスキーを削除しました。'
  } catch (value) { error.value = errorMessage(value) }
  finally { securityPending.value = false }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>プロフィール</h1>
    <p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <template v-if="profile">
      <form class="form-stack" @submit.prevent="saveProfile">
        <label class="field">ニックネーム<input v-model="profile.display_name" required maxlength="40"></label>
        <label class="field">X<input v-model="profile.x_handle" maxlength="15" placeholder="@なしで入力"></label>
        <label class="field-check"><input v-model="profile.x_handle_visible" type="checkbox">プロフィールにX IDを表示する</label>
        <label class="field">Instagram<input v-model="profile.instagram_handle" maxlength="30" placeholder="@なしで入力"></label>
        <label class="field-check"><input v-model="profile.instagram_handle_visible" type="checkbox">プロフィールにInstagram IDを表示する</label>
        <button class="button">プロフィールを保存</button>
      </form>
      <hr>
      <details class="security-settings">
        <summary><strong>セキュリティ設定</strong><span>パスワード・パスキー</span></summary>
        <div class="security-settings-body">
          <form class="form-stack" @submit.prevent="changePassword">
            <h2>パスワード変更</h2>
            <p class="helper">変更後はすべての端末からログアウトします。</p>
            <label class="field">現在のパスワード<input v-model="currentPassword" type="password" autocomplete="current-password" required></label>
            <label class="field">新しいパスワード<input v-model="newPassword" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
            <label class="field">新しいパスワード（確認）<input v-model="newPasswordAgain" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
            <button class="button" :disabled="securityPending">パスワードを変更</button>
          </form>
          <hr>
          <div class="form-stack">
            <h2>パスキー</h2>
            <p class="helper">Face ID、Touch ID、Windows Helloなどでログインできます。複数の端末を登録できます。</p>
            <label class="field">確認用パスワード<input v-model="passkeyPassword" type="password" autocomplete="current-password"></label>
            <label class="field">新しいパスキー名<input v-model="passkeyName" maxlength="80" placeholder="例：iPhone、Windows Hello"></label>
            <button class="button" type="button" :disabled="securityPending" @click="addPasskey">パスキーを追加</button>
            <div v-if="passkeys.length" class="passkey-list">
              <article v-for="passkey in passkeys" :key="passkey.public_id" class="passkey-card">
                <label class="field">名前<input v-model="passkey.name" maxlength="80"></label>
                <p class="helper">登録：{{ formatJapaneseDateTime(passkey.created_at) }}<br>最終利用：{{ passkey.last_used_at ? formatJapaneseDateTime(passkey.last_used_at) : 'まだ利用していません' }}</p>
                <div class="actions"><button class="button secondary" type="button" @click="savePasskeyName(passkey)">名前を保存</button><button class="button danger" type="button" :disabled="securityPending" @click="deletePasskey(passkey)">削除</button></div>
              </article>
            </div>
            <p v-else class="notice">登録済みのパスキーはありません。</p>
          </div>
        </div>
      </details>
      <hr>
      <form v-if="seasons.length && privacyMatrix" class="form-stack" @submit.prevent="savePrivacy">
        <h2>予定の公開設定</h2>
        <label class="field">設定するシーズン<select v-model="selectedSeasonId" @change="loadPrivacy"><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option></select></label>
        <p class="helper">シーズンごとに、公開範囲別の情報を選択できます。より広い範囲で許可した情報は、それより内側の範囲にも表示されます。</p>
        <p class="notice">{{ customized ? 'このシーズン専用の設定です。' : '現在は標準設定を使用しています。保存するとシーズン専用になります。' }}</p>
        <div class="privacy-table-wrap">
          <table class="privacy-table">
            <thead><tr><th>公開範囲</th><th>すべて</th><th v-for="field in fields" :key="field.key">{{ field.label }}</th></tr></thead>
            <tbody>
              <tr v-for="audience in audiences" :key="audience.key">
                <th><span>{{ audience.label }}</span><small>{{ audience.help }}</small></th>
                <td><input type="checkbox" :checked="allSelected(audience.key)" :aria-label="`${audience.label}のすべて`" @change="toggleAll(audience.key)"></td>
                <td v-for="field in fields" :key="field.key"><input v-model="privacyMatrix[audience.key][field.key]" type="checkbox" :aria-label="`${audience.label}の${field.label}`"></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="helper">保存すると、選択したシーズンの登録済み予定にも即時適用されます。</p>
        <div class="actions"><button class="button">このシーズンの設定を保存</button><button v-if="customized" class="button secondary" type="button" @click="resetPrivacy">標準設定に戻す</button></div>
      </form>
      <p v-else class="notice">現在、公開設定を変更できるシーズンがありません。</p>
    </template>
  </section>
</template>
