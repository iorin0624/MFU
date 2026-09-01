<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { portalApi } from '@/api/client';
import type { ChatRoom, ChatRoomMember } from '@/types';

const props = defineProps<{eventId:number; rooms:ChatRoom[]}>();
const emit = defineEmits<{changed:[]} >();
const candidates = ref<ChatRoomMember[]>([]);
const selectedRoom = ref('');
const roomName = ref('');
const members = ref<string[]>([]);
const memberQuery = ref('');
const busy = ref(false);
const error = ref('');
const filteredCandidates = computed(() => {
  const query = memberQuery.value.trim().toLocaleLowerCase('ja');
  if (!query) return candidates.value;
  return candidates.value.filter((item) => `${item.display_name} ${item.actor_key}`.toLocaleLowerCase('ja').includes(query));
});

async function loadCandidates() {
  const response = await portalApi.chatRoomMemberCandidates(props.eventId);
  candidates.value = response.candidates || [];
}
async function selectRoom(roomId: string) {
  selectedRoom.value = roomId;
  const room = props.rooms.find((item) => item.room_id === roomId);
  roomName.value = room?.room_name || '';
  if (!roomId) { members.value = []; return; }
  const response = await portalApi.chatRoomMembers(props.eventId, roomId);
  members.value = (response.members || []).map((item) => item.actor_key);
}
async function createRoom() {
  if (!roomName.value.trim()) return;
  await run(async () => { await portalApi.chatCreateRoom(props.eventId, roomName.value.trim(), members.value); roomName.value=''; members.value=[]; emit('changed'); });
}
async function saveRoom() {
  if (!selectedRoom.value || !roomName.value.trim()) return;
  await run(async () => { await portalApi.chatUpdateRoom(props.eventId, selectedRoom.value, roomName.value.trim()); await portalApi.chatSetRoomMembers(props.eventId, selectedRoom.value, members.value); emit('changed'); });
}
async function deleteRoom() {
  if (!selectedRoom.value || !confirm('このサブルームを終了しますか？過去のメッセージは保持されます。')) return;
  await run(async () => { await portalApi.chatDeleteRoom(props.eventId, selectedRoom.value); selectedRoom.value=''; roomName.value=''; members.value=[]; emit('changed'); });
}
async function run(task:()=>Promise<void>) {
  busy.value=true; error.value='';
  try { await task(); } catch (reason) { error.value=reason instanceof Error ? reason.message : '処理に失敗しました。'; }
  finally { busy.value=false; }
}
onMounted(()=>{ void loadCandidates().catch((reason)=>{ error.value=reason instanceof Error?reason.message:'参加者を取得できませんでした。'; }); });
</script>

<template>
  <div class="chat-room-manager">
    <div class="field"><label>編集するサブルーム</label><select :value="selectedRoom" @change="selectRoom(($event.target as HTMLSelectElement).value)"><option value="">新規作成</option><option v-for="room in rooms.filter((item)=>!item.is_main)" :key="room.room_id" :value="room.room_id">{{ room.room_name }}</option></select></div>
    <div class="field"><label>ルーム名</label><input v-model="roomName" maxlength="80"></div>
    <fieldset><legend>参加者</legend><div class="field"><label for="chat-room-member-search">名前検索</label><input id="chat-room-member-search" v-model="memberQuery" type="search" placeholder="名前を入力"></div><div class="form-actions"><button type="button" class="button secondary compact" :disabled="busy || !candidates.length" @click="members=[...new Set(candidates.map(item=>item.actor_key))]">全選択</button><button type="button" class="button secondary compact" :disabled="busy || !members.length" @click="members=[]">全解除</button></div><label v-for="candidate in filteredCandidates" :key="candidate.actor_key" class="check-row"><input v-model="members" type="checkbox" :value="candidate.actor_key"><span>{{ candidate.display_name }}</span></label><p v-if="!filteredCandidates.length" class="muted">該当する参加者はいません。</p></fieldset>
    <div v-if="error" class="alert error">{{ error }}</div>
    <div class="form-actions"><button v-if="selectedRoom" class="button danger" :disabled="busy" @click="deleteRoom">終了</button><button class="button primary" :disabled="busy||!roomName.trim()" @click="selectedRoom?saveRoom():createRoom()">{{ selectedRoom?'保存':'作成' }}</button></div>
  </div>
</template>
