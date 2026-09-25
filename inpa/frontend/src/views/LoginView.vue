<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { getPasskey, passkeysSupported } from '@/lib/webauthn'

const router = useRouter(); const route = useRoute(); const email = ref(''); const password = ref(''); const remember = ref(false)
const pending = ref(false); const error = ref('')
async function submit() {
  pending.value = true; error.value = ''
  try { await api('/auth/login', { method: 'POST', body: { email: email.value, password: password.value, remember: remember.value } }); await router.push('/') }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '通信に失敗しました。' }
  finally { pending.value = false }
}
async function loginWithPasskey() {
  if (!passkeysSupported()) { error.value = 'このブラウザではパスキーを利用できません。'; return }
  pending.value = true; error.value = ''
  try {
    const start = await api<{ challenge_id: string; options: Parameters<typeof getPasskey>[0] }>('/auth/passkeys/authentication/options', { method: 'POST' })
    const credential = await getPasskey(start.options)
    await api('/auth/passkeys/authentication/complete', { method: 'POST', body: { challenge_id: start.challenge_id, credential, remember: remember.value } })
    await router.push('/')
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '通信に失敗しました。'
  } finally { pending.value = false }
}
</script>

<template>
  <section class="auth-card">
    <h1>ログイン</h1>
    <p v-if="route.query.password_changed === '1'" class="notice">パスワードを変更しました。新しいパスワードでログインしてください。</p>
    <form class="form-stack" @submit.prevent="submit">
      <label class="field">メールアドレス<input v-model="email" type="email" autocomplete="email" required></label>
      <label class="field">パスワード<input v-model="password" type="password" autocomplete="current-password" required></label>
      <label class="field-check"><input v-model="remember" type="checkbox">この端末でログインを保持する</label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="button" :disabled="pending">{{ pending ? 'ログイン中…' : 'ログイン' }}</button>
    </form>
    <hr>
    <button class="button secondary" :disabled="pending" @click="loginWithPasskey">パスキーでログイン</button>
  </section>
</template>
