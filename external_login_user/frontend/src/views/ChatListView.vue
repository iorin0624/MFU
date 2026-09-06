<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import { useChatStore } from '@/stores/chat';
import { useRoute } from 'vue-router';
import { formatDateTime } from '@/utils/format';
import { chatPushState, disableChatPush, enableChatPush } from '@/services/chatPush';

const chat = useChatStore();
const route = useRoute();
const router = useRouter();
const pushEnabled = ref(false); const pushBusy = ref(false); const pushMessage = ref('');
const dmSettingsOpen=ref(false); const dmSettingsBusy=ref(false); const dmUserUser=ref(false); const dmAdminKey=ref('admin:1');
const eventItems = computed(() => chat.events.map((item) => ({
  ...item,
  uuid: item.event_uuid || '',
})));

async function togglePush() {
  pushBusy.value=true; pushMessage.value='';
  try { pushEnabled.value ? await disableChatPush() : await enableChatPush(); pushEnabled.value=!pushEnabled.value; pushMessage.value=pushEnabled.value?'通知を有効にしました。':'通知を停止しました。'; }
  catch (reason) { pushMessage.value=reason instanceof Error?reason.message:'通知設定に失敗しました。'; }
  finally { pushBusy.value=false; }
}
async function openDm(item:{dm_uuid:string;peer_actor_key:string}) {
  let dmUuid=item.dm_uuid;
  if(!dmUuid) dmUuid=(await portalApi.chatDmOpen(item.peer_actor_key)).dm_uuid;
  if(dmUuid)await router.push({name:'chat-dm',params:{dmUuid},query:route.query.auth_scope==='mfu'?{auth_scope:'mfu'}:{}});
}
function openDmSettings() { const settings=chat.bootstrap?.dm_settings;dmUserUser.value=Boolean(settings?.enable_user_user);dmAdminKey.value=settings?.admin_actor_key||'admin:1';dmSettingsOpen.value=true; }
async function saveDmSettings() { dmSettingsBusy.value=true;try{await portalApi.chatDmSettings(dmUserUser.value,dmAdminKey.value);await chat.loadBootstrap(true);dmSettingsOpen.value=false;}finally{dmSettingsBusy.value=false;} }
onMounted(() => { void chat.loadBootstrap(true); void chatPushState().then((value)=>{pushEnabled.value=value;}); });
</script>

<template>
  <section class="page-heading chat-page-heading">
    <div><p class="eyebrow">CHAT</p><h1>チャット</h1><p>イベントの連絡と個別メッセージを確認できます。</p></div>
    <div class="chat-heading-actions"><span :class="['realtime-status', { online: chat.connected }]">{{ chat.connected ? 'リアルタイム接続中' : '再接続中' }}</span><button class="button secondary compact" :disabled="pushBusy" @click="togglePush">{{ pushEnabled?'通知を停止':'通知を有効化' }}</button></div>
  </section>
  <p v-if="pushMessage" class="chat-push-message">{{ pushMessage }}</p>
  <LoadingBlock v-if="chat.loading && !chat.bootstrap">チャットを読み込んでいます</LoadingBlock>
  <div v-if="chat.error" class="alert error">{{ chat.error }}</div>
  <div v-if="chat.bootstrap" class="chat-index-layout">
    <section class="panel chat-index-panel">
      <h2>イベントチャット</h2>
      <div class="chat-destination-list">
        <button v-for="item in eventItems" :key="item.id" type="button" :disabled="!item.uuid" @click="router.push({name:'event-chat',params:{uuid:item.uuid},query:route.query.auth_scope==='mfu'?{auth_scope:'mfu'}:{}})">
          <span class="destination-icon">💬</span>
          <span><strong>{{ item.title }}</strong><small v-if="item.start_at">{{ formatDateTime(item.start_at) }}</small></span>
          <b v-if="item.unread_count" class="chat-count">{{ item.unread_count > 99 ? '99+' : item.unread_count }}</b><i>›</i>
        </button>
        <p v-if="!eventItems.length" class="empty-inline">利用できるイベントチャットはありません。</p>
      </div>
    </section>
    <section v-if="chat.dms.length || chat.bootstrap.actor.is_chat_admin_alias" class="panel chat-index-panel">
      <h2>個別メッセージ <button v-if="chat.bootstrap.dm_settings?.can_manage" type="button" class="inline-action" @click="openDmSettings">設定</button></h2>
      <div class="chat-destination-list">
        <button v-for="item in chat.dms" :key="item.dm_uuid || item.peer_actor_key" type="button" @click="openDm(item)">
          <span class="destination-icon">👤</span>
          <span><strong>{{ item.peer_display_name }}</strong><small>{{ item.last_message || 'メッセージを開く' }}</small></span>
          <b v-if="item.unread_count" class="chat-count">{{ item.unread_count > 99 ? '99+' : item.unread_count }}</b><i>›</i>
        </button>
        <p v-if="!chat.dms.length" class="empty-inline">個別メッセージはありません。</p>
      </div>
    </section>
  </div>
  <div v-if="dmSettingsOpen" class="modal-backdrop" @click.self="dmSettingsOpen=false"><section class="modal-card compact-modal"><h2>個別メッセージ設定</h2><label class="check-row"><input v-model="dmUserUser" type="checkbox">参加者同士の個別メッセージを有効化</label><label class="field"><span>管理者 actor_key</span><input v-model.trim="dmAdminKey" maxlength="128"></label><div class="form-actions"><button class="button secondary" @click="dmSettingsOpen=false">キャンセル</button><button class="button primary" :disabled="dmSettingsBusy" @click="saveDmSettings">保存</button></div></section></div>
</template>
