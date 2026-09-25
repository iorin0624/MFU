<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'

const route = useRoute(); const router = useRouter()
const token = computed(() => typeof route.query.token === 'string' ? route.query.token : '')
const displayName = ref(''); const password = ref(''); const passwordAgain = ref('')
const xHandle = ref(''); const instagramHandle = ref('')
const xVisible = ref(false); const instagramVisible = ref(false)
const termsAccepted = ref(false); const privacyAccepted = ref(false)
const termsId = ref(''); const privacyId = ref(''); const pending = ref(false); const error = ref('')

onMounted(async () => {
  try {
    const result = await api<{ documents: Record<string, { public_id: string }> }>('/legal/documents')
    termsId.value = result.documents.terms?.public_id ?? ''
    privacyId.value = result.documents.privacy?.public_id ?? ''
  } catch { error.value = '利用規約を読み込めませんでした。' }
})

async function submit() {
  error.value = ''
  if (!token.value) { error.value = '登録リンクが見つかりません。メール内のリンクを開いてください。'; return }
  if (password.value !== passwordAgain.value) { error.value = 'パスワードが一致しません。'; return }
  if (!xHandle.value.trim() && !instagramHandle.value.trim()) { error.value = 'X IDまたはInstagram IDのどちらかを入力してください。'; return }
  pending.value = true
  try {
    await api('/auth/register/complete', { method: 'POST', body: {
      token: token.value, password: password.value, display_name: displayName.value,
      x_handle: xHandle.value, instagram_handle: instagramHandle.value,
      x_handle_visible: xVisible.value, instagram_handle_visible: instagramVisible.value,
      terms_accepted: termsAccepted.value, privacy_accepted: privacyAccepted.value,
      terms_document_id: termsId.value, privacy_document_id: privacyId.value,
    } })
    await router.push('/onboarding')
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '通信に失敗しました。' } finally { pending.value = false }
}
</script>

<template>
  <section class="auth-card">
    <p class="wizard-progress">初回設定 1 / 3</p>
    <h1>プロフィール作成</h1>
    <p class="helper">最初に、INPAで使用するプロフィールとパスワードを設定します。</p>
    <form class="form-stack" @submit.prevent="submit">
      <label class="field">ニックネーム<input v-model="displayName" autocomplete="nickname" required maxlength="40"></label>
      <p class="help">X IDまたはInstagram IDのどちらかは必須です。</p>
      <label class="field">X ID<input v-model="xHandle" autocomplete="off" maxlength="16" placeholder="@username"></label>
      <label class="field-check"><input v-model="xVisible" type="checkbox">プロフィールにX IDを表示する</label>
      <label class="field">Instagram ID<input v-model="instagramHandle" autocomplete="off" maxlength="31" placeholder="@username"></label>
      <label class="field-check"><input v-model="instagramVisible" type="checkbox">プロフィールにInstagram IDを表示する</label>
      <label class="field">パスワード<input v-model="password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
      <label class="field">パスワード（確認）<input v-model="passwordAgain" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
      <label class="field-check"><input v-model="termsAccepted" type="checkbox" required><RouterLink to="/legal/terms" target="_blank">利用規約</RouterLink>に同意します。</label>
      <label class="field-check"><input v-model="privacyAccepted" type="checkbox" required><RouterLink to="/legal/privacy" target="_blank">プライバシーポリシー</RouterLink>に同意します。</label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="button" :disabled="pending">{{ pending ? '登録中…' : '保存して次へ' }}</button>
    </form>
  </section>
</template>
