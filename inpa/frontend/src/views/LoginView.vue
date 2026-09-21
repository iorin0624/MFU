<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'

const router = useRouter(); const email = ref(''); const password = ref(''); const remember = ref(false)
const pending = ref(false); const error = ref('')
async function submit() {
  pending.value = true; error.value = ''
  try { await api('/auth/login', { email: email.value, password: password.value, remember: remember.value }); await router.push('/') }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '通信に失敗しました。' }
  finally { pending.value = false }
}
</script>

<template>
  <section class="auth-card">
    <h1>ログイン</h1>
    <form class="form-stack" @submit.prevent="submit">
      <label class="field">メールアドレス<input v-model="email" type="email" autocomplete="email" required></label>
      <label class="field">パスワード<input v-model="password" type="password" autocomplete="current-password" required></label>
      <label class="field-check"><input v-model="remember" type="checkbox">この端末でログインを保持する</label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="button" :disabled="pending">{{ pending ? 'ログイン中…' : 'ログイン' }}</button>
    </form>
  </section>
</template>
