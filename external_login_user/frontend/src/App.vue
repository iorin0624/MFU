<script setup lang="ts">
import { onMounted } from 'vue';
import AppHeader from '@/components/AppHeader.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import EmptyState from '@/components/EmptyState.vue';
import PortalFooter from '@/components/PortalFooter.vue';
import PortalUtilities from '@/components/PortalUtilities.vue';
import { runtimeConfig } from '@/config';
import { usePortalStore } from '@/stores/portal';

const store = usePortalStore();
onMounted(() => store.bootstrap());
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
    <EmptyState
      v-else-if="store.session && !store.session.authenticated"
      icon="🔐"
      title="ログインが必要です"
      text="イベント情報を見るにはLINEログインまたはMFUログインを行ってください。"
    >
      <a class="button primary" :href="runtimeConfig.loginUrl">ログイン画面へ</a>
    </EmptyState>
    <div v-else-if="store.session?.prerequisites.emailVerificationRequired" class="alert warning">
      <strong>メール認証が必要です</strong>
      <span>登録済みメールアドレスの確認を完了してからご利用ください。</span>
      <a class="button secondary" href="/external-login/unverified">メール認証へ</a>
    </div>
    <div v-else-if="store.session?.prerequisites.privacyAgreementRequired" class="alert warning">
      <strong>プライバシーポリシーへの同意が必要です</strong>
      <a class="button secondary" href="/external-login/">確認画面へ</a>
    </div>
    <template v-else-if="store.ready"><RouterView /><PortalUtilities /></template>
  </main>
  <PortalFooter v-if="store.session?.authenticated" />
</template>
