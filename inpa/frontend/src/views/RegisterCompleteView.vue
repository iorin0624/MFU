<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'

const route = useRoute(); const router = useRouter()
const token = computed(() => typeof route.query.token === 'string' ? route.query.token : '')
const displayName = ref(''); const password = ref(''); const passwordAgain = ref('')
const termsAccepted = ref(false); const pending = ref(false); const error = ref('')

async function submit() {
  error.value = ''
  if (!token.value) { error.value = '登録リンクが見つかりません。メール内のリンクを開いてください。'; return }
  if (password.value !== passwordAgain.value) { error.value = 'パスワードが一致しません。'; return }
  pending.value = true
  try {
    await api('/auth/register/complete', { method: 'POST', body: { token: token.value, password: password.value, terms_accepted: termsAccepted.value, display_name: displayName.value } })
    await router.push('/')
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '通信に失敗しました。' } finally { pending.value = false }
}
</script>

<template>
  <section class="auth-card">
    <h1>登録を完了する</h1>
    <form class="form-stack" @submit.prevent="submit">
      <label class="field">表示名<input v-model="displayName" autocomplete="nickname" required maxlength="40"></label>
      <label class="field">パスワード<input v-model="password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
      <label class="field">パスワード（確認）<input v-model="passwordAgain" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label>
      <label class="field-check"><input v-model="termsAccepted" type="checkbox" required>利用規約とプライバシーポリシーに同意します。</label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="button" :disabled="pending">{{ pending ? '登録中…' : '登録を完了する' }}</button>
    </form>
  </section>
</template>
