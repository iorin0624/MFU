<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import ChatRoomPane from '@/components/ChatRoomPane.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { useChatStore } from '@/stores/chat';
const route=useRoute(); const router=useRouter(); const chat=useChatStore();
onMounted(()=>{ void chat.openDm(String(route.params.dmUuid)); });
onBeforeUnmount(()=>{ chat.resetRoom(); chat.unbindRealtime(); });
</script>
<template><section class="chat-dm-page"><div class="event-chat-toolbar"><button class="event-chat-back" type="button" @click="router.push('/chat')"><span class="wide-label">← チャット一覧へ</span><span class="short-label">← 一覧</span></button></div><LoadingBlock v-if="chat.loading&&!chat.currentDmUuid">DMを読み込んでいます</LoadingBlock><ChatRoomPane v-else-if="chat.currentDmUuid" /></section></template>
