<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'

const route = useRoute(); const router = useRouter()
const authenticated = ref(false); const authChecked = ref(false)
const logoutPending = ref(false); const logoutError = ref('')

async function refreshAuth() {
  try {
    await api<{ user: { public_id: string; display_name: string } }>('/auth/me')
    authenticated.value = true
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) authenticated.value = false
  } finally { authChecked.value = true }
}

async function logout() {
  logoutPending.value = true; logoutError.value = ''
  try {
    await api('/auth/logout', { method: 'POST' })
    authenticated.value = false
    await router.push('/login')
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) {
      authenticated.value = false
      await router.push('/login')
    } else logoutError.value = cause instanceof ApiError ? cause.message : 'ログアウトに失敗しました。'
  } finally { logoutPending.value = false }
}

watch(() => route.fullPath, refreshAuth, { immediate: true })
</script>

<template>
  <main class="app-shell">
    <header class="site-header">
      <RouterLink to="/" class="brand">INPA</RouterLink>
      <nav aria-label="主なメニュー">
        <template v-if="authChecked && authenticated">
          <RouterLink to="/profile">プロフィール</RouterLink>
          <RouterLink to="/visits">予定</RouterLink>
          <RouterLink to="/calendar">カレンダー</RouterLink>
          <RouterLink to="/share">共有</RouterLink>
          <RouterLink to="/connections">つながり</RouterLink>
          <button class="nav-button" type="button" :disabled="logoutPending" @click="logout">{{ logoutPending ? '処理中…' : 'ログアウト' }}</button>
        </template>
        <template v-else-if="authChecked">
          <RouterLink to="/login">ログイン</RouterLink>
          <RouterLink to="/register">新規登録</RouterLink>
        </template>
      </nav>
    </header>
    <p v-if="logoutError" class="error" role="alert">{{ logoutError }}</p>
    <RouterView />
  </main>
</template>
