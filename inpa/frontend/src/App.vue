<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDateTime } from '@/lib/date'

type Release = { public_id: string; version: string; change_type: 'major' | 'feature' | 'fix'; title: string; content_markdown: string; published_at: string }

const route = useRoute(); const router = useRouter()
const authenticated = ref(false); const authChecked = ref(false)
const logoutPending = ref(false); const logoutError = ref('')
const currentVersion = ref(''); const releaseNotice = ref<Release | null>(null)
const unseenCount = ref(0); const dontShowRelease = ref(false); const releaseError = ref('')
const releasePending = ref(false)
const releaseLabels = { major: 'メジャー', feature: '機能追加', fix: '修正' }

async function loadLatestVersion() {
  try { currentVersion.value = (await api<{ current_version: string | null }>('/releases/latest')).current_version ?? '' } catch { /* footer remains available without a version */ }
}

async function loadUnseenRelease() {
  try {
    const result = await api<{ unseen_count: number; release: Release | null }>('/releases/unseen')
    unseenCount.value = result.unseen_count
    if (result.release && sessionStorage.getItem(`inpa-release-hidden:${result.release.version}`) !== '1') releaseNotice.value = result.release
  } catch { /* authentication and legal consent flows retain priority */ }
}

async function closeReleaseNotice() {
  if (!releaseNotice.value) return
  releasePending.value = true; releaseError.value = ''
  try {
    if (dontShowRelease.value) await api(`/releases/${releaseNotice.value.public_id}/dismiss`, { method: 'POST' })
    sessionStorage.setItem(`inpa-release-hidden:${releaseNotice.value.version}`, '1')
    releaseNotice.value = null; dontShowRelease.value = false
  } catch (cause) {
    releaseError.value = cause instanceof ApiError ? cause.message : '設定を保存できませんでした。'
  } finally { releasePending.value = false }
}

function viewAllUpdates() {
  if (releaseNotice.value) sessionStorage.setItem(`inpa-release-hidden:${releaseNotice.value.version}`, '1')
  releaseNotice.value = null
}

async function refreshAuth() {
  try {
    const result = await api<{ user: { public_id: string; connection_id: string; display_name: string; legal_consent_required: boolean } }>('/auth/me')
    authenticated.value = true
    if (!result.user.legal_consent_required && route.path !== '/legal/consent') {
      const onboarding = await api<{ required: boolean }>('/onboarding')
      if (!onboarding.required) await loadUnseenRelease()
    }
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) { authenticated.value = false; releaseNotice.value = null }
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
onMounted(loadLatestVersion)
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
          <RouterLink :to="{ path: '/feedback', query: route.path === '/feedback' ? {} : { from: route.fullPath } }">フィードバック</RouterLink>
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
    <footer class="site-footer"><RouterLink to="/legal/terms">利用規約</RouterLink><RouterLink to="/legal/privacy">プライバシーポリシー</RouterLink><RouterLink to="/updates">アップデート情報</RouterLink><span v-if="currentVersion">INPA v{{ currentVersion }}</span></footer>
    <div v-if="releaseNotice" class="release-overlay" role="presentation">
      <section class="release-modal" role="dialog" aria-modal="true" aria-labelledby="release-title">
        <span class="update-kind">{{ releaseLabels[releaseNotice.change_type] }}</span>
        <h2 id="release-title">v{{ releaseNotice.version }} {{ releaseNotice.title }}</h2>
        <p class="helper">{{ formatJapaneseDateTime(releaseNotice.published_at) }}<template v-if="unseenCount > 1">／未確認 {{ unseenCount }}件</template></p>
        <p class="update-content">{{ releaseNotice.content_markdown }}</p>
        <RouterLink to="/updates" @click="viewAllUpdates">すべてのアップデートを見る</RouterLink>
        <label class="field-check release-dismiss"><input v-model="dontShowRelease" type="checkbox">次回からこのアップデートを表示しない</label>
        <p v-if="releaseError" class="error">{{ releaseError }}</p>
        <button class="button" type="button" :disabled="releasePending" @click="closeReleaseNotice">{{ releasePending ? '保存中…' : '閉じる' }}</button>
      </section>
    </div>
  </main>
</template>
