<script setup lang="ts">
import { usePortalStore } from '@/stores/portal';
const store = usePortalStore();
</script>
<template>
  <footer class="portal-footer">
    <nav aria-label="各種ドキュメント">
      <a v-if="store.session?.documents.privacyPolicyUrl" :href="store.session.documents.privacyPolicyUrl" target="_blank" rel="noopener">プライバシーポリシー</a>
      <a v-if="store.session?.documents.commerceLawUrl" :href="store.session.documents.commerceLawUrl" target="_blank" rel="noopener">特定商取引法に基づく表記</a>
      <a v-if="store.session?.documents.participantTermsUrl" :href="store.session.documents.participantTermsUrl" target="_blank" rel="noopener">参加・支払・キャンセル規定</a>
    </nav>
    <span>Mimoria by IORI0624 MFU System</span>
  </footer>
</template>
