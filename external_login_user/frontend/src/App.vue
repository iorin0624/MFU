<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import AppHeader from '@/components/AppHeader.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import EmptyState from '@/components/EmptyState.vue';
import PortalFooter from '@/components/PortalFooter.vue';
import PortalUtilities from '@/components/PortalUtilities.vue';
import { usePortalStore } from '@/stores/portal';
import { startNotificationRealtime, stopNotificationRealtime } from '@/services/notificationRealtime';

const store = usePortalStore();
const route = useRoute();
const publicRoute = computed(() => route.name === 'login');
const verificationRoute = computed(() => route.name === 'email-verify');
onMounted(async () => {
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
</script>

<template>
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
      <RouterLink class="button primary" to="/login">ログイン画面へ</RouterLink>
    </EmptyState>
    <RouterView v-else-if="verificationRoute" />
    <div v-else-if="store.session?.prerequisites.emailVerificationRequired" class="alert warning">
      <strong>メール認証が必要です</strong>
      <span>登録済みメールアドレスの確認を完了してからご利用ください。</span>
      <RouterLink class="button secondary" to="/email-verify">メール認証へ</RouterLink>
    </div>
    <div v-else-if="store.session?.prerequisites.privacyAgreementRequired" class="alert warning">
      <strong>プライバシーポリシーへの同意が必要です</strong>
      <a class="button secondary" href="/external-login/">確認画面へ</a>
    </div>
    <template v-else-if="store.ready"><RouterView /><PortalUtilities /></template>
  </main>
  <PortalFooter v-if="store.session?.authenticated" />
</template>
