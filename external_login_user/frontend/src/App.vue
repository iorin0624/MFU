<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import AppHeader from '@/components/AppHeader.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import EmptyState from '@/components/EmptyState.vue';
import PortalFooter from '@/components/PortalFooter.vue';
import PortalUtilities from '@/components/PortalUtilities.vue';
import { usePortalStore } from '@/stores/portal';
import { portalApi } from '@/api/client';
import { startNotificationRealtime, stopNotificationRealtime } from '@/services/notificationRealtime';
import { trackChatViewport } from '@/utils/chatViewport';

const store = usePortalStore();
const route = useRoute();
const loginRoute = computed(() => ({ name: 'login', query: { next: route.fullPath } }));
const publicRoute = computed(() => route.name === 'login');
const verificationRoute = computed(() => route.name === 'email-verify');
const profileRoute = computed(() => route.name === 'profile');
const privacyAccepted = ref(false);
const privacyBusy = ref(false);
const privacyError = ref('');
const immersiveChatRoute = computed(() => route.name === 'event-chat' || route.name === 'chat-dm');
const viewportStyle = ref<Record<string, string>>({});
const shortChatViewport = computed(() => parseFloat(viewportStyle.value['--chat-viewport-height'] || '1000') < 420);
let stopViewportTracking: (() => void) | undefined;
onMounted(async () => {
  stopViewportTracking = trackChatViewport((style) => { viewportStyle.value = style; });
  await store.bootstrap();
  if (store.session?.authenticated) {
    startNotificationRealtime(
      store.session.notificationScope,
      () => store.refreshUnread(),
      (counts) => store.applyUnread(counts),
    );
  }
});
onBeforeUnmount(stopNotificationRealtime);
onBeforeUnmount(() => stopViewportTracking?.());
async function agreePrivacy() {
  if (!privacyAccepted.value || privacyBusy.value) return;
  privacyBusy.value=true; privacyError.value='';
  try { await portalApi.agreePrivacyPolicy(); await store.bootstrap(true); }
  catch (reason) { privacyError.value=reason instanceof Error?reason.message:'同意内容を保存できませんでした。'; }
  finally { privacyBusy.value=false; }
}
</script>

<template>
  <div class="portal-app" :class="{'immersive-chat': immersiveChatRoute, 'chat-short-viewport': immersiveChatRoute && shortChatViewport}" :style="viewportStyle">
  <AppHeader v-if="store.session" />
  <main class="app-main">
    <LoadingBlock v-if="store.loading && !store.ready">イベント情報を読み込んでいます</LoadingBlock>
    <div v-else-if="store.error" class="alert error">
      <strong>読み込みに失敗しました</strong>
      <span>{{ store.error }}</span>
      <button type="button" class="button secondary" @click="store.bootstrap(true)">再読み込み</button>
    </div>
    <RouterView v-else-if="publicRoute" />
    <EmptyState
      v-else-if="store.session && !store.session.authenticated"
      icon="🔐"
      title="ログインが必要です"
      text="イベント情報を見るにはLINEログインまたはメールPIN認証を行ってください。"
    >
      <RouterLink class="button primary" :to="loginRoute">ログイン画面へ</RouterLink>
    </EmptyState>
    <div v-else-if="store.session?.prerequisites.privacyAgreementRequired" class="alert warning">
      <strong>プライバシーポリシーへの同意が必要です</strong>
      <span>内容を確認し、同意してからご利用ください。</span>
      <a v-if="store.session.documents.privacyPolicyUrl" class="button secondary" :href="store.session.documents.privacyPolicyUrl" target="_blank" rel="noopener">プライバシーポリシーを確認</a>
      <label class="check-row"><input v-model="privacyAccepted" type="checkbox">内容を確認し、プライバシーポリシーに同意します</label>
      <button type="button" class="button primary" :disabled="!privacyAccepted || privacyBusy" @click="agreePrivacy">{{ privacyBusy ? '保存中…' : '同意して続ける' }}</button>
      <span v-if="privacyError" class="danger-text">{{ privacyError }}</span>
    </div>
    <RouterView v-else-if="profileRoute || verificationRoute" />
    <div v-else-if="store.session?.prerequisites.profileCompletionRequired" class="alert warning">
      <strong>プロフィール登録が必要です</strong><span>利用を開始するため、プロフィールを登録してください。</span><RouterLink class="button secondary" to="/profile">プロフィール登録へ</RouterLink>
    </div>
    <div v-else-if="store.session?.prerequisites.emailVerificationRequired" class="alert warning">
      <strong>メール認証が必要です</strong><span>登録済みメールアドレスの確認を完了してからご利用ください。</span><RouterLink class="button secondary" to="/email-verify">メール認証へ</RouterLink>
    </div>
    <template v-else-if="store.ready"><RouterView /><PortalUtilities v-if="!immersiveChatRoute" /></template>
  </main>
  <PortalFooter v-if="store.session?.authenticated && !immersiveChatRoute" />
  </div>
</template>
