<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import TurnstileWidget from '@/components/TurnstileWidget.vue'
import { ApiError, api } from '@/lib/api'

const route = useRoute()
const invitationToken = ref(typeof route.query.invite === 'string' ? route.query.invite : '')
const email = ref('')
const turnstileToken = ref('')
const pending = ref(false)
const message = ref('')
const error = ref('')

async function submit() {
  pending.value = true; message.value = ''; error.value = ''
  try {
    const result = await api<{ message: string }>('/auth/register/request', { method: 'POST', body: { invitation_token: invitationToken.value, email: email.value, turnstile_token: turnstileToken.value } })
    message.value = result.message
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '通信に失敗しました。'
  } finally { pending.value = false }
}
</script>

<template>
  <section class="auth-card">
    <h1>新規登録</h1>
    <p>管理者から発行された招待トークンが必要です。メールに届くリンクから登録を完了してください。</p>
    <form class="form-stack" @submit.prevent="submit">
      <label class="field">招待トークン<input v-model.trim="invitationToken" autocomplete="off" required maxlength="128"></label>
      <label class="field">メールアドレス<input v-model="email" type="email" autocomplete="email" required maxlength="254"></label>
      <TurnstileWidget @token="turnstileToken = $event" />
      <p v-if="message" class="notice">{{ message }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="button" :disabled="pending">{{ pending ? '送信中…' : '確認メールを送る' }}</button>
    </form>
  </section>
</template>
