<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, api } from '@/lib/api'

const route = useRoute()
const category = ref('')
const message = ref('')
const pending = ref(false)
const error = ref('')
const success = ref('')
const remaining = computed(() => 2000 - message.value.length)

function sourcePath(): string {
  const from = typeof route.query.from === 'string' ? route.query.from : ''
  return from.startsWith('/') && !from.startsWith('//') ? from : '/feedback'
}

async function submit() {
  error.value = ''; success.value = ''
  if (!category.value) { error.value = '項目を選択してください。'; return }
  if (message.value.trim().length < 10 || message.value.trim().length > 2000) {
    error.value = '内容は10〜2000文字で入力してください。'; return
  }
  pending.value = true
  try {
    const result = await api<{ message: string }>('/feedback', {
      method: 'POST', body: { category: category.value, message: message.value, source_path: sourcePath() },
    })
    success.value = result.message
    category.value = ''; message.value = ''
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '送信に失敗しました。'
  } finally { pending.value = false }
}
</script>

<template>
  <section class="panel">
    <h1>フィードバック</h1>
    <p>不具合やご要望をお寄せください。ログイン中の利用者ID・表示名・メールアドレスと、送信日時・送信元ページ・ブラウザー情報を運営へ送信します。</p>
    <p v-if="success" class="notice" role="status">{{ success }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form @submit.prevent="submit">
      <label>項目
        <select v-model="category" required>
          <option value="" disabled>選択してください</option>
          <option value="bug">不具合・エラー</option>
          <option value="feature">機能の要望</option>
          <option value="usability">操作性・使いやすさ</option>
          <option value="wording">文言・表示</option>
          <option value="other">その他</option>
        </select>
      </label>
      <label>内容
        <textarea v-model="message" rows="10" minlength="10" maxlength="2000" required placeholder="具体的な状況やご要望を入力してください"></textarea>
      </label>
      <p class="helper">10〜2000文字（残り {{ remaining }} 文字）</p>
      <button class="button" type="submit" :disabled="pending">{{ pending ? '送信中…' : '送信する' }}</button>
    </form>
    <p class="helper">迷惑送信防止のため、送信は5分間に3件までです。送信後の編集・削除はできません。</p>
  </section>
</template>
