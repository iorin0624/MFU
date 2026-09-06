<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import ChatRoomPane from '@/components/ChatRoomPane.vue';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import { useChatStore } from '@/stores/chat';
import ChatRoomManager from '@/components/ChatRoomManager.vue';
import { eventThemeStyle, useDocumentEventTheme } from '@/utils/eventTheme';

const route = useRoute(); const router = useRouter(); const chat = useChatStore();
const eventId = ref(0); const error = ref('');
const activeEvent = computed(() => chat.events.find((item) => item.id === eventId.value));
useDocumentEventTheme(computed(() => activeEvent.value?.theme_color));
const managerOpen = ref(false); const muted = ref(false);
async function load(roomId = '') {
  try { await chat.loadBootstrap(); const event = chat.events.find((item) => item.event_uuid === String(route.params.uuid)); if (!event) throw new Error('このイベントのチャットを利用する権限がありません。'); eventId.value = event.id; await chat.openEvent(eventId.value, roomId); muted.value=Boolean(chat.activeRoom?.muted_until && new Date(chat.activeRoom.muted_until).getTime()>Date.now()); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : 'チャットを開けませんでした。'; }
}
function backToEvent() {
  if (route.query.auth_scope === 'mfu') window.location.assign(`/external-login/admin/events/${eventId.value}`);
  else void router.push({name:'event',params:{uuid:route.params.uuid}});
}
async function toggleMute() { if (!chat.activeRoom) return; await portalApi.chatMuteRoom(eventId.value,chat.activeRoom.room_id,muted.value?undefined:24); muted.value=!muted.value; }
function visibilityChanged() { if (chat.currentEventId && chat.activeRoom) void portalApi.chatPresence('ping',chat.currentEventId,chat.activeRoom.room_id,chat.presenceClientId,!document.hidden).catch(()=>undefined); }
onMounted(() => {
  document.addEventListener('visibilitychange',visibilityChanged);
  const roomId = Array.isArray(route.query.room_id) ? route.query.room_id[0] : route.query.room_id;
  void load(String(roomId || ''));
});
onBeforeUnmount(()=>{ document.removeEventListener('visibilitychange',visibilityChanged); chat.resetRoom(); chat.unbindRealtime(); });
</script>
<template>
  <section class="event-chat-page event-theme" :style="eventThemeStyle(activeEvent?.theme_color)">
    <div class="event-chat-toolbar">
      <button class="event-chat-back" type="button" @click="backToEvent"><span class="wide-label">← イベント詳細へ</span><span class="short-label">← 詳細</span></button>
      <div v-if="chat.currentEventId" class="chat-page-actions">
        <button class="button secondary compact" @click="toggleMute"><span class="wide-label">{{ muted?'通知を再開':'24時間ミュート' }}</span><span class="short-label">{{ muted?'🔔 再開':'🔕 24h' }}</span></button>
        <button v-if="chat.canManageRooms" class="button secondary compact" @click="managerOpen=true"><span class="wide-label">サブルーム管理</span><span class="short-label">⚙ ルーム</span></button>
      </div>
    </div>
    <LoadingBlock v-if="chat.loading && !chat.currentEventId">チャットを読み込んでいます</LoadingBlock>
    <div v-else-if="error" class="alert error">{{ error }}</div>
    <ChatRoomPane v-else-if="chat.currentEventId" @room-change="load" />
    <div v-if="managerOpen" class="modal-backdrop" @click.self="managerOpen=false"><section class="modal-card"><h2>サブルーム管理</h2><ChatRoomManager :event-id="eventId" :rooms="chat.rooms" @changed="load();managerOpen=false"/><div class="form-actions"><button class="button secondary" @click="managerOpen=false">閉じる</button></div></section></div>
  </section>
</template>
