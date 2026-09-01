<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { portalApi } from '@/api/client';
import { useChatStore } from '@/stores/chat';
import type { ChatMessage } from '@/types';
import { shouldSendOnKey } from '@/utils/chatPresentation';
import sendIcon from '@/assets/chat-send.png';
import { resizeChatComposer } from '@/utils/chatComposer';
import { onPortalEvent, onPortalResume, onPortalConnection } from '@/services/portalRealtime';
import { createRefreshQueue } from '@/utils/refreshQueue';

const chat = useChatStore();
const emit = defineEmits<{roomChange:[roomId:string]} >();
const body = ref('');
const composerInput = ref<HTMLTextAreaElement | null>(null);
const roomShell = ref<HTMLElement | null>(null);
const latestJumpBottom = ref(78);
let composerObserver: ResizeObserver | undefined;
let composerFrame = 0;
function scheduleComposerResize() {
  if (composerFrame) return;
  composerFrame = window.requestAnimationFrame(() => {
    composerFrame = 0;
    if (!composerInput.value || !roomShell.value || !messageList.value) return;
    const mobile = window.matchMedia('(max-width: 560px)').matches
      || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    latestJumpBottom.value = resizeChatComposer(composerInput.value, roomShell.value, messageList.value, mobile);
  });
}
watch(body, scheduleComposerResize, { flush:'post' });
const messageList = ref<HTMLElement | null>(null);
const files = ref<HTMLInputElement | null>(null);
const uploadBusy = ref(false);
const pendingFiles = ref<File[]>([]);
const pendingPreviews = ref<Array<{name:string;url:string}>>([]);
const searchOpen = ref(false);
const searchQuery = ref('');
const searchResults = ref<Array<Record<string, any>>>([]);
const thread = ref<{root:ChatMessage;replies:ChatMessage[]}|null>(null);
const threadBody = ref('');
const threadError = ref('');
const threadLoading = ref(false);
const selectionCopy = ref<ChatMessage | null>(null);
const selectionText = ref<HTMLTextAreaElement | null>(null);
let threadGeneration = 0;
let threadRootId = 0;
const threadCleanup: Array<() => void> = [];
async function refreshThread() {
  if (!threadRootId || !chat.activeRoom || document.hidden) return;
  const generation = threadGeneration;
  try {
    const response = await portalApi.chatThreads(chat.currentEventId, chat.activeRoom.room_id, threadRootId);
    if (generation !== threadGeneration) return;
    thread.value = { root: response.root, replies: response.replies || [] };
    threadError.value = '';
  } catch {
    if (generation === threadGeneration) threadError.value = 'スレッドを更新できませんでした。再試行してください。';
  } finally {
    if (generation === threadGeneration) threadLoading.value = false;
  }
}
const threadRefresh = createRefreshQueue(refreshThread);
function closeThread() { ++threadGeneration; threadRootId = 0; thread.value = null; threadBody.value = ''; threadError.value = ''; threadLoading.value = false; }
async function openSelectionCopy(message: ChatMessage) {
  selectionCopy.value = message;
  closeMessageMenu();
  await nextTick();
  selectionText.value?.focus();
}
watch(() => `${chat.currentDmUuid}|${chat.currentEventId}|${chat.activeRoom?.room_id || ''}`, () => { closeThread(); selectionCopy.value = null; });
onBeforeUnmount(() => { closeThread(); threadRefresh.stop(); threadCleanup.forEach((dispose) => dispose()); });
const reactionDetails = ref<{emoji:string;actors:Array<Record<string, any>>}|null>(null);
const lightboxItems = ref<Array<{url:string}>>([]);
const lightboxIndex = ref(0);
const showLatest = ref(false);
const showUnreadJump = ref(false);
const mentionCandidates = ref<Array<{actor_key:string;display_name:string}>>([]);
const messageMenu = ref<{message:ChatMessage;x:number;y:number;mobile:boolean}|null>(null);
const readDetails = ref<{messageId:number;actors:string[]}|null>(null);
const swipeVisual = ref<{messageId:number;offset:number;active:boolean;armed:boolean}>({messageId:0,offset:0,active:false,armed:false});
let typingTimer = 0;
let longPressTimer = 0;
let swipeResetTimer = 0;
let gestureStart: {x:number;y:number;message:ChatMessage;pointerId:number}|null = null;
let longPressOpened = false;

function nearBottom() {
  const element = messageList.value;
  return !element || element.scrollHeight - element.scrollTop - element.clientHeight < 180;
}
async function scrollBottom(force = false) {
  const element = messageList.value;
  if (!element || (!force && !nearBottom())) return;
  await nextTick(); element.scrollTop = element.scrollHeight;
}
async function positionInitialMessages() {
  await nextTick();
  await new Promise<void>((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve())));
  const element=messageList.value; if(!element)return;
  const unreadId=chat.firstUnreadMessageId;
  const unread=unreadId ? element.querySelector<HTMLElement>(`[data-chat-message-id="${unreadId}"]`) : null;
  if(unread){
    element.scrollTop=Math.max(0,unread.offsetTop-76);
    chat.consumeUnreadBoundary();
    showUnreadJump.value=false;
  } else {
    element.scrollTop=element.scrollHeight;
    showUnreadJump.value=false;
  }
  showLatest.value=!nearBottom();
}
watch(() => chat.messages.length, async (_value, oldValue) => {
  if(oldValue===0){ await positionInitialMessages(); return; }
  const wasNear = nearBottom();
  await nextTick();
  if (wasNear) { if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight; showLatest.value=false; }
  else showLatest.value=true;
});
watch(() => `${chat.currentDmUuid}|${chat.currentEventId}|${chat.activeRoom?.room_id || ''}`, () => { void positionInitialMessages(); }, {flush:'post'});
function listScrolled() {
  chat.markSeen();
  messageMenu.value=null;
  showLatest.value=!nearBottom();
  const element=messageList.value;
  const unreadId=chat.firstUnreadMessageId;
  const unread=unreadId&&element ? element.querySelector<HTMLElement>(`[data-chat-message-id="${unreadId}"]`) : null;
  if(!unread){ showUnreadJump.value=false; return; }
  const listRect=element!.getBoundingClientRect();
  const unreadRect=unread.getBoundingClientRect();
  const unreadVisible=unreadRect.bottom>listRect.top&&unreadRect.top<listRect.bottom;
  showUnreadJump.value=!unreadVisible;
  if(unreadVisible)chat.consumeUnreadBoundary();
}
function jumpLatest() { if (messageList.value) messageList.value.scrollTo({top:messageList.value.scrollHeight,behavior:'smooth'}); showLatest.value=false; }
function closeReactionMenus(except?:HTMLDetailsElement) {
  document.querySelectorAll<HTMLDetailsElement>('.chat-reaction-trigger[open]').forEach((details)=>{ if(details!==except)details.open=false; });
}
function reactionMenuToggled(event:Event) { const details=event.currentTarget as HTMLDetailsElement; if(details.open)closeReactionMenus(details); }
function outsideReactionPointer(event:PointerEvent) { if(!(event.target as Element | null)?.closest('.chat-reaction-trigger'))closeReactionMenus(); }
function reactFromMenu(message:ChatMessage,emoji:string,event:MouseEvent) { chat.react(message,emoji); const details=(event.currentTarget as HTMLElement).closest('details') as HTMLDetailsElement | null; if(details)details.open=false; }

function keepTypingVisible() {
  if (editing.value) return;
  chat.setTyping(true);
  window.clearTimeout(typingTimer);
  // 少し考えて入力が止まっただけでは表示を消さず、最後の入力から5秒維持する。
  typingTimer = window.setTimeout(() => chat.setTyping(false), 5000);
}
async function inputChanged() {
  if (editing.value) { mentionCandidates.value=[]; return; }
  // 日本語IMEの変換開始直後は、入力中でも一時的にbodyが空になる。
  // 空文字を終了条件にせず、最後の入力操作から5秒後のタイマーだけで終了する。
  keepTypingVisible();
  const match=body.value.match(/(?:^|\s)@([^\s@]{0,30})$/);
  if(match && chat.currentEventId && chat.activeRoom) {
    try { mentionCandidates.value=(await portalApi.chatMentionCandidates(chat.currentEventId,chat.activeRoom.room_id,match[1])).candidates||[]; }
    catch { mentionCandidates.value=[]; }
  } else mentionCandidates.value=[];
}
function chooseMention(name:string) { body.value=body.value.replace(/(?:^|\s)@([^\s@]{0,30})$/, (whole)=>`${whole.startsWith(' ')?' ':''}@${name} `); mentionCandidates.value=[]; }
async function send() {
  if (editing.value) { await saveEdit(); return; }
  if (editBusy.value || chat.sending || uploadBusy.value) return;
  const value = body.value;
  if (!value.trim()) return;
  body.value = ''; chat.setTyping(false); await chat.send(value); void scrollBottom(true);
}
function composerKeydown(event: KeyboardEvent) {
  const mobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (shouldSendOnKey(event, mobile)) { event.preventDefault(); void send(); }
}
function clearPendingImages() {
  pendingPreviews.value.forEach((item) => URL.revokeObjectURL(item.url));
  pendingPreviews.value = []; pendingFiles.value = [];
  if (files.value) files.value.value = '';
}
function removePendingImage(index:number) {
  const preview=pendingPreviews.value[index]; if(preview)URL.revokeObjectURL(preview.url);
  pendingPreviews.value.splice(index,1); pendingFiles.value.splice(index,1);
}
function selectImages(selected: FileList | File[] | null) {
  if (editing.value || editBusy.value) return;
  if (!selected?.length || (!chat.currentEventId && !chat.currentDmUuid)) return;
  clearPendingImages();
  pendingFiles.value = [...selected].filter((file) => file.type.startsWith('image/')).slice(0, chat.bootstrap?.limits.upload_max_files || 6);
  pendingPreviews.value = pendingFiles.value.map((file) => ({ name:file.name, url:URL.createObjectURL(file) }));
}
async function upload() {
  if (editing.value || editBusy.value || uploadBusy.value) return;
  const list = [...pendingFiles.value];
  if (!list.length) return;
  uploadBusy.value = true;
  try {
    if (chat.currentDmUuid) await portalApi.chatDmUploadImages(chat.currentDmUuid, list, body.value);
    else if (chat.activeRoom) await portalApi.chatUploadImages(chat.currentEventId, chat.activeRoom.room_id, list, body.value);
    body.value=''; chat.replyTo=null;
  }
  catch (reason) { chat.error = reason instanceof Error ? reason.message : '画像を送信できませんでした。'; }
  finally { uploadBusy.value = false; clearPendingImages(); }
}
function dropImages(event: DragEvent) { selectImages(event.dataTransfer?.files || null); }
const editing = ref<{message:ChatMessage;dmUuid:string;eventId:number;roomId:string}|null>(null);
const editError = ref('');
const editBusy = ref(false);
let beforeEdit: {body:string;reply:ChatMessage|null}|null = null;
let editGeneration = 0;
async function edit(message: ChatMessage) {
  if (editBusy.value || uploadBusy.value || chat.sending) return;
  if (!editing.value) beforeEdit={body:body.value,reply:chat.replyTo || null};
  ++editGeneration;
  editing.value = {message,dmUuid:chat.currentDmUuid || '',eventId:chat.currentEventId || 0,roomId:chat.activeRoom?.room_id || ''};
  body.value=message.editable_text ?? message.body_plain; editError.value='';
  chat.replyTo=null; mentionCandidates.value=[];
  window.clearTimeout(typingTimer); chat.setTyping(false);
  await nextTick(); scheduleComposerResize(); composerInput.value?.focus();
}
function finishEdit(restore=true) {
  ++editGeneration;
  editing.value=null; editError.value=''; editBusy.value=false;
  body.value=restore ? beforeEdit?.body || '' : '';
  chat.replyTo=restore ? beforeEdit?.reply || null : null;
  beforeEdit=null; scheduleComposerResize();
}
function closeEdit() { if (!editBusy.value) finishEdit(); }
watch(() => `${chat.currentDmUuid}|${chat.currentEventId}|${chat.activeRoom?.room_id || ''}`, () => {
  if (editing.value) { finishEdit(false); clearPendingImages(); }
});
async function saveEdit() {
  if (!editing.value || editBusy.value || !body.value.trim()) return;
  const target=editing.value;
  const generation=editGeneration;
  const value=body.value;
  editBusy.value=true; editError.value='';
  try {
    if (target.dmUuid) await portalApi.chatDmEdit(target.dmUuid,target.message.id,value);
    else await portalApi.chatEditMessage(target.eventId,target.roomId,target.message.id,value);
    if (generation===editGeneration) finishEdit();
  } catch (reason) { if (generation===editGeneration) editError.value=reason instanceof Error ? reason.message : '編集を保存できませんでした。'; }
  finally { if (generation===editGeneration) editBusy.value=false; }
}
async function remove(message: ChatMessage, mode:'cancel'|'admin'='cancel') {
  if (!window.confirm(mode==='admin' ? '管理者権限で削除しますか？管理者による削除として表示されます。' : 'このメッセージの送信を取り消しますか？')) return;
  try {
    if (chat.currentDmUuid) await portalApi.chatDmDelete(chat.currentDmUuid, message.id, mode);
    else if (chat.currentEventId && chat.activeRoom) await portalApi.chatDeleteMessage(chat.currentEventId, chat.activeRoom.room_id, message.id, mode);
  } catch (reason) { chat.error=reason instanceof Error ? reason.message : '取り消しできませんでした。'; }
}
async function search() {
  if (!searchQuery.value.trim()) return;
  const response = chat.currentDmUuid
    ? await portalApi.chatDmSearch(chat.currentDmUuid, searchQuery.value)
    : chat.currentEventId && chat.activeRoom ? await portalApi.chatSearch(chat.currentEventId, chat.activeRoom.room_id, searchQuery.value) : {results:[]};
  searchResults.value = response.results || [];
}
async function openThread(message: ChatMessage) {
  if (!chat.currentEventId || !chat.activeRoom) return;
  closeThread();
  threadRootId = message.thread_root_id || message.id;
  thread.value = { root: message, replies: [] };
  threadLoading.value = true;
  threadRefresh.schedule();
}
function sendThread() {
  if (!thread.value || !threadBody.value.trim()) return;
  chat.sendThread(threadBody.value, thread.value.root); threadBody.value = '';
}
async function showReactionDetails(message: ChatMessage, emoji: string) {
  const response = chat.currentDmUuid
    ? await portalApi.chatDmReactionDetails(chat.currentDmUuid, message.id)
    : chat.activeRoom ? await portalApi.chatReactionDetails(chat.currentEventId, chat.activeRoom.room_id, message.id) : {groups:[]};
  const group=(response.groups || []).find((item) => item.emoji === emoji);
  reactionDetails.value = { emoji, actors:group?.actors || [] };
}
function jumpTo(messageId: number) {
  searchOpen.value = false;
  nextTick(() => document.querySelector(`[data-chat-message-id="${messageId}"]`)?.scrollIntoView({behavior:'smooth',block:'center'}));
}
function jumpUnread() {
  if(chat.firstUnreadMessageId)jumpTo(chat.firstUnreadMessageId);
  chat.consumeUnreadBoundary();
  showUnreadJump.value=false;
}
function openLightbox(message:ChatMessage,index:number) { lightboxItems.value=(message.images||[]).map((item)=>({url:item.url}));lightboxIndex.value=index; }
function moveLightbox(delta:number) { if(!lightboxItems.value.length)return;lightboxIndex.value=(lightboxIndex.value+delta+lightboxItems.value.length)%lightboxItems.value.length; }
function closeLightbox() { lightboxItems.value=[];lightboxIndex.value=0; }
async function copyMessage(message:ChatMessage) { try { await navigator.clipboard.writeText(message.body_plain||''); } catch { chat.error='クリップボードへコピーできませんでした。'; } }
function isCoarsePointer() { return window.matchMedia('(pointer: coarse)').matches; }
function isInteractiveTarget(event:Event) { return Boolean((event.target as HTMLElement | null)?.closest('button,a,input,textarea,select,summary')); }
function closeMessageMenu() { messageMenu.value=null; }
function openMessageMenu(message:ChatMessage,event?:MouseEvent) {
  if(message.deleted_flag)return;
  const mobile=isCoarsePointer();
  const width=232; const height=330;
  messageMenu.value={message,mobile,x:mobile?0:Math.max(8,Math.min(event?.clientX||0,window.innerWidth-width-8)),y:mobile?0:Math.max(8,Math.min(event?.clientY||0,window.innerHeight-height-8))};
}
function messageMenuStyle() { const menu=messageMenu.value; return !menu||menu.mobile?{}:{left:`${menu.x}px`,top:`${menu.y}px`}; }
function replyToMessage(message:ChatMessage) { if(editBusy.value)return; if(editing.value)closeEdit(); chat.replyTo=message; closeMessageMenu(); }
function messageDoubleClick(message:ChatMessage,event:MouseEvent) { if(!isCoarsePointer()&&!isInteractiveTarget(event))replyToMessage(message); }
function clearSwipeVisual() {
  window.clearTimeout(swipeResetTimer);
  const messageId=swipeVisual.value.messageId;
  swipeVisual.value={messageId,offset:0,active:false,armed:false};
  swipeResetTimer=window.setTimeout(()=>{ if(!swipeVisual.value.active&&swipeVisual.value.offset===0)swipeVisual.value={messageId:0,offset:0,active:false,armed:false}; },220);
}
function cancelMessageGesture() { window.clearTimeout(longPressTimer); gestureStart=null; longPressOpened=false; clearSwipeVisual(); }
function swipeClass(message:ChatMessage) { return { 'is-swiping':swipeVisual.value.messageId===message.id&&swipeVisual.value.active,'swipe-armed':swipeVisual.value.messageId===message.id&&swipeVisual.value.armed }; }
function swipeStyle(message:ChatMessage) { return swipeVisual.value.messageId===message.id?{transform:`translate3d(${swipeVisual.value.offset}px,0,0)`}:{}; }
function swipeIndicatorStyle(message:ChatMessage) { const offset=swipeVisual.value.messageId===message.id?Math.abs(swipeVisual.value.offset):0; return {opacity:String(Math.min(1,offset/55)),transform:`scale(${0.72+Math.min(1,offset/55)*0.28})`}; }
function messagePointerDown(message:ChatMessage,event:PointerEvent) {
  if(event.pointerType!=='touch'||isInteractiveTarget(event)||message.deleted_flag)return;
  window.clearTimeout(swipeResetTimer);
  gestureStart={x:event.clientX,y:event.clientY,message,pointerId:event.pointerId}; longPressOpened=false;
  swipeVisual.value={messageId:message.id,offset:0,active:true,armed:false};
  try { (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId); } catch { /* pointer capture is optional */ }
  window.clearTimeout(longPressTimer);
  longPressTimer=window.setTimeout(()=>{ if(!gestureStart)return; longPressOpened=true; clearSwipeVisual(); openMessageMenu(message); },520);
}
function messagePointerMove(event:PointerEvent) {
  const start=gestureStart; if(!start||start.pointerId!==event.pointerId)return;
  const dx=event.clientX-start.x; const dy=event.clientY-start.y;
  if(Math.abs(dx)>10||Math.abs(dy)>10)window.clearTimeout(longPressTimer);
  if(Math.abs(dy)>Math.abs(dx)&&Math.abs(dy)>9){ gestureStart=null; clearSwipeVisual(); return; }
  const directed=start.message.is_me?Math.min(0,dx):Math.max(0,dx);
  const limited=Math.sign(directed)*Math.min(78,Math.abs(directed));
  swipeVisual.value={messageId:start.message.id,offset:limited,active:true,armed:Math.abs(limited)>=55};
}
function messagePointerUp(message:ChatMessage,event:PointerEvent) {
  const start=gestureStart; window.clearTimeout(longPressTimer);
  if(!start||start.pointerId!==event.pointerId||longPressOpened){gestureStart=null;longPressOpened=false;clearSwipeVisual();return;}
  const dx=event.clientX-start.x; const dy=event.clientY-start.y;
  const shouldReply=Math.abs(dy)<=35&&((!message.is_me&&dx>=55)||(message.is_me&&dx<=-55));
  clearSwipeVisual();
  if(shouldReply)replyToMessage(message);
  gestureStart=null;
}
function openReadDetails(message:ChatMessage) { readDetails.value={messageId:message.id,actors:chat.readersFor(message.id)}; }
function dateLabel(message:ChatMessage) { return message.created_at_jst_date_label || new Date(message.created_at_iso).toLocaleDateString('ja-JP'); }
function showDateDivider(index:number) { return index===0 || dateLabel(chat.messages[index-1])!==dateLabel(chat.messages[index]); }
onMounted(() => {
  for (const event of ['chat_message', 'chat_edit_update', 'chat_delete_update', 'chat_reaction_update']) {
    threadCleanup.push(onPortalEvent(event, (payload: any) => {
      if (threadRootId && chat.matches(payload)) threadRefresh.schedule();
    }));
  }
  threadCleanup.push(onPortalResume(() => threadRefresh.schedule()));
  threadCleanup.push(onPortalConnection((connected) => { if (connected) threadRefresh.schedule(); }));
  document.addEventListener('pointerdown',outsideReactionPointer);
  composerObserver = new ResizeObserver(scheduleComposerResize);
  if (roomShell.value) composerObserver.observe(roomShell.value);
  if (messageList.value) composerObserver.observe(messageList.value);
  scheduleComposerResize();
  void positionInitialMessages();
});
onBeforeUnmount(() => { composerObserver?.disconnect(); window.cancelAnimationFrame(composerFrame); document.removeEventListener('pointerdown',outsideReactionPointer); window.clearTimeout(typingTimer); window.clearTimeout(longPressTimer); window.clearTimeout(swipeResetTimer); clearPendingImages(); chat.setTyping(false); });
</script>

<template>
  <section ref="roomShell" class="chat-room-shell">
    <header class="chat-room-header">
      <div><strong>{{ chat.currentTitle }}</strong><small v-if="chat.activeRoom">{{ chat.activeRoom.room_name }}</small></div>
      <div class="chat-room-tools">
        <select v-if="chat.rooms.length > 1" :value="chat.activeRoom?.room_id" aria-label="ルームを選択" @change="emit('roomChange', ($event.target as HTMLSelectElement).value)">
          <option v-for="room in chat.rooms" :key="room.room_id" :value="room.room_id">{{ room.room_name }}{{ room.unread_count ? `（未読${room.unread_count}）` : '' }}</option>
        </select>
        <button type="button" title="検索" @click="searchOpen = true">🔎</button>
      </div>
    </header>
    <div v-if="chat.error" class="chat-inline-error">{{ chat.error }} <button @click="chat.error=''">×</button></div>
    <div v-if="chat.recoveryError" class="chat-inline-error">{{ chat.recoveryError }} <button type="button" :disabled="chat.recoveryBusy" @click="chat.recoverCurrent()">再試行</button></div>
    <button v-if="showUnreadJump" type="button" class="chat-unread-jump" @click="jumpUnread">未読メッセージへ ↓</button>
    <div ref="messageList" class="chat-message-list" @scroll.passive="listScrolled" @dragover.prevent @drop.prevent="dropImages">
      <button v-if="chat.hasMore" type="button" class="load-older" :disabled="chat.loading" @click="chat.loadOlder()">{{ chat.loading ? '読み込み中…' : '以前のメッセージを表示' }}</button>
      <template v-for="(message,messageIndex) in chat.messages" :key="message.id">
      <div v-if="showDateDivider(messageIndex)" class="chat-date-divider"><span>{{ dateLabel(message) }}</span></div>
      <article :data-chat-message-id="message.id" :class="['chat-message-row',{mine:message.is_me,deleted:message.deleted_flag}]" @contextmenu.prevent="openMessageMenu(message,$event)" @dblclick="messageDoubleClick(message,$event)" @pointerdown="messagePointerDown(message,$event)" @pointermove="messagePointerMove" @pointerup="messagePointerUp(message,$event)" @pointercancel="cancelMessageGesture">
        <img :src="message.sender_avatar_url || chat.bootstrap?.default_avatar_url" alt="" class="chat-avatar">
        <div class="chat-message-content">
          <div class="chat-message-meta"><strong>{{ message.sender_display_name }}</strong><time>{{ message.created_at_jst_time_hm }}</time></div>
          <button v-if="message.reply_to_message_id" type="button" class="chat-reply-quote" @click="jumpTo(message.reply_to_message_id)">
            <strong>{{ message.reply_to_sender_display_name }}</strong><span>{{ message.reply_to_body_plain_excerpt }}</span>
          </button>
          <span v-if="swipeVisual.messageId===message.id" :class="['chat-swipe-reply-indicator',{armed:swipeVisual.armed}]" :style="swipeIndicatorStyle(message)" aria-hidden="true">↩</span>
          <div :class="['chat-bubble-line',swipeClass(message)]" :style="swipeStyle(message)"><div class="chat-bubble">
            <div v-if="message.images?.length" class="chat-image-grid">
              <button v-for="(image,imageIndex) in message.images" :key="image.seq" type="button" @click="openLightbox(message,imageIndex)"><img :src="image.thumb_url" alt="送信画像"></button>
            </div>
            <p v-if="message.deleted_flag">{{message.body_plain}}</p>
            <p v-else-if="message.body_plain" v-html="message.body_html || message.body_plain"></p>
            <small v-if="message.edited_flag">編集済み</small>
          </div><details v-if="!message.deleted_flag" class="chat-reaction-trigger" @toggle="reactionMenuToggled"><summary title="リアクションを追加">＋</summary><div><button v-for="emoji in chat.bootstrap?.reaction_emojis" :key="emoji" type="button" @click="reactFromMenu(message,emoji,$event)">{{ emoji }}</button></div></details></div>
          <div v-if="(!message.deleted_flag && message.reactions_summary?.length) || (message.is_me && chat.readersFor(message.id).length)" class="chat-message-footer">
          <div v-if="!message.deleted_flag && message.reactions_summary?.length" class="chat-reactions">
            <button v-for="reaction in message.reactions_summary" :key="reaction.emoji" type="button" title="リアクションした人を表示" @click="showReactionDetails(message,reaction.emoji)">{{ reaction.emoji }} {{ reaction.count }}</button>
          </div>
          <button v-if="message.is_me && chat.readersFor(message.id).length" type="button" class="chat-readers" @click="openReadDetails(message)">既読 {{ chat.readersFor(message.id).length }}</button>
          </div>
        </div>
      </article>
      </template>
      <p v-if="!chat.messages.length && !chat.loading" class="empty-inline">まだメッセージはありません。</p>
    </div>
    <button v-if="showLatest" type="button" class="chat-latest-jump" :style="{bottom:`${latestJumpBottom}px`}" @click="jumpLatest">↓ 最新へ</button>
    <div v-if="chat.typingNames.length" class="chat-typing">{{ chat.typingNames.join('、') }}さんが入力中…</div>
    <div v-if="editing" class="chat-compose-context" role="status"><span><strong>{{editBusy?'保存中…':'✎ メッセージを編集中'}}</strong></span><button type="button" aria-label="編集を取り消す" :disabled="editBusy" @click="closeEdit">取消 ×</button></div>
    <div v-if="editing && editError" class="chat-inline-error" role="alert">{{editError}}</div>
    <div v-if="!editing && chat.replyTo" class="chat-compose-context"><span><strong>{{ chat.replyTo.sender_display_name }}</strong>へ返信：{{ chat.replyTo.body_plain }}</span><button type="button" @click="chat.replyTo=null">×</button></div>
    <div v-if="!editing && pendingPreviews.length" class="chat-upload-preview">
      <strong>{{ pendingPreviews.length }}枚を送信します</strong>
      <div><figure v-for="(item,index) in pendingPreviews" :key="item.url"><button type="button" aria-label="この画像を取り消す" @click="removePendingImage(index)">×</button><img :src="item.url" :alt="item.name"><figcaption>{{ item.name }}</figcaption></figure></div>
      <p><button class="button secondary compact" type="button" :disabled="uploadBusy" @click="clearPendingImages">取り消す</button><button class="button primary compact" type="button" :disabled="uploadBusy" @click="upload">{{ uploadBusy ? '送信中…' : '画像を送信' }}</button></p>
    </div>
    <form class="chat-composer" @submit.prevent="send" @dragover.prevent @drop.prevent="dropImages">
      <div v-if="mentionCandidates.length" class="chat-mention-menu"><button v-for="candidate in mentionCandidates" :key="candidate.actor_key" type="button" @click="chooseMention(candidate.display_name)">@{{ candidate.display_name }}</button></div>
      <label class="chat-file-button" :class="{disabled:uploadBusy || !!editing}">📷<input ref="files" type="file" accept="image/*" multiple :disabled="uploadBusy || !!editing" @change="selectImages(($event.target as HTMLInputElement).files)"></label>
      <textarea ref="composerInput" v-model="body" rows="1" :maxlength="chat.bootstrap?.limits.message_max_len || 2000" :placeholder="editing?'メッセージを編集':'メッセージを入力'" :aria-label="editing?'編集するメッセージ':'メッセージを入力'" :disabled="editBusy" enterkeyhint="enter" @beforeinput="keepTypingVisible" @compositionstart="keepTypingVisible" @input="inputChanged" @keydown="composerKeydown"></textarea>
      <button class="chat-send-button" type="submit" :aria-label="editing?'編集を保存':'送信'" :title="editing?'編集を保存（PC：Alt+Enter）':'送信（PC：Alt+Enter）'" :disabled="!body.trim() || chat.sending || editBusy || uploadBusy"><img :src="sendIcon" alt=""></button>
    </form>
  </section>

  <div v-if="messageMenu" class="chat-message-menu-backdrop" @click="closeMessageMenu">
    <section :class="['chat-message-menu',{mobile:messageMenu.mobile}]" :style="messageMenuStyle()" role="menu" @click.stop>
      <header><strong>メッセージ操作</strong><button type="button" aria-label="閉じる" @click="closeMessageMenu">×</button></header>
      <button type="button" @click="replyToMessage(messageMenu.message)">返信</button>
      <button v-if="messageMenu.message.body_plain" type="button" @click="copyMessage(messageMenu.message);closeMessageMenu()">コピー</button>
      <button v-if="messageMenu.message.body_plain" type="button" @click="openSelectionCopy(messageMenu.message)">選択コピー</button>
      <button v-if="chat.currentEventId" type="button" @click="openThread(messageMenu.message);closeMessageMenu()">スレッド<span v-if="messageMenu.message.thread_reply_count">（{{ messageMenu.message.thread_reply_count }}件）</span></button>
      <button v-if="messageMenu.message.can_edit" type="button" @click="edit(messageMenu.message);closeMessageMenu()">編集</button>
      <button v-if="messageMenu.message.can_delete" type="button" class="danger-text" @click="remove(messageMenu.message);closeMessageMenu()">送信取消</button>
      <button v-if="messageMenu.message.can_admin_delete" type="button" class="danger-text" @click="remove(messageMenu.message,'admin');closeMessageMenu()">管理者として削除</button>
      <div class="chat-message-menu-reactions"><span>リアクション</span><button v-for="emoji in chat.bootstrap?.reaction_emojis" :key="emoji" type="button" @click="chat.react(messageMenu.message,emoji);closeMessageMenu()">{{ emoji }}</button></div>
    </section>
  </div>

  <div v-if="searchOpen" class="modal-backdrop" @click.self="searchOpen=false">
    <section class="modal-card chat-search-modal"><h2>メッセージ検索</h2><div class="chat-search-form"><input v-model="searchQuery" @keydown.enter="search"><button class="button primary compact" @click="search">検索</button></div><button v-for="item in searchResults" :key="item.message_id" class="chat-search-result" @click="jumpTo(Number(item.message_id))"><strong>{{ item.sender_display_name || item.sender }}</strong><span>{{ item.excerpt }}</span></button><div class="form-actions"><button class="button secondary" @click="searchOpen=false">閉じる</button></div></section>
  </div>
  <div v-if="thread" class="modal-backdrop" @click.self="closeThread" @keydown.esc="closeThread">
    <section class="modal-card chat-thread-modal" role="dialog" aria-modal="true" aria-label="スレッド"><h2>スレッド</h2><p v-if="threadLoading">読み込み中…</p><div v-if="threadError" class="alert error">{{ threadError }} <button @click="threadRefresh.schedule">再試行</button></div><article v-for="message in [thread.root,...thread.replies]" :key="message.id" class="thread-message" :class="{mine:message.is_me,deleted:message.deleted_flag}"><header><strong>{{ message.sender_display_name }}</strong><time>{{ message.created_at_jst_time_hm }}</time></header><button v-if="message.reply_to_message_id" type="button" class="chat-reply-quote" @click="jumpTo(message.reply_to_message_id)"><strong>{{ message.reply_to_sender_display_name }}</strong><span>{{ message.reply_to_body_plain_excerpt }}</span></button><div v-if="message.images?.length" class="chat-images"><button v-for="(image,index) in message.images" :key="image.seq" type="button" @click="openLightbox(message,index)"><img :src="image.thumb_url" alt="送信画像"></button></div><p v-if="message.body_plain" :class="{'chat-deleted-text':message.deleted_flag}" v-html="message.body_html || message.body_plain"></p><small v-if="message.edited_flag">編集済み</small><footer v-if="!message.deleted_flag && message.reactions_summary?.length"><button v-for="reaction in message.reactions_summary" :key="reaction.emoji" type="button" @click="showReactionDetails(message,reaction.emoji)">{{ reaction.emoji }} {{ reaction.count }}</button></footer></article><form class="chat-search-form" @submit.prevent="sendThread"><textarea v-model="threadBody" rows="2" placeholder="スレッドへ返信" @keydown="shouldSendOnKey($event,isCoarsePointer()) && ($event.preventDefault(),sendThread())"></textarea><button class="button primary compact" :disabled="threadLoading || !threadBody.trim()">送信</button></form><div class="form-actions"><button class="button secondary" @click="closeThread">閉じる</button></div></section>
  </div>
  <div v-if="selectionCopy" class="modal-backdrop" @click.self="selectionCopy=null" @keydown.esc="selectionCopy=null">
    <section class="modal-card selection-copy-modal" role="dialog" aria-modal="true" aria-label="選択コピー"><h2>選択コピー</h2><p>必要な部分を選択し、端末のコピー操作を使用してください。</p><textarea ref="selectionText" :value="selectionCopy.body_plain" readonly aria-label="コピーするメッセージ" spellcheck="false"></textarea><div class="form-actions"><button class="button secondary" @click="selectionText?.select()">全選択</button><button class="button secondary" @click="selectionCopy=null">閉じる</button></div></section>
  </div>
  <div v-if="reactionDetails" class="modal-backdrop" @click.self="reactionDetails=null"><section class="modal-card compact-modal"><h2>{{ reactionDetails.emoji }} リアクション</h2><ul><li v-for="actor in reactionDetails.actors" :key="actor.actor_key || actor.actor_id">{{ actor.display_name || actor.actor_key }}</li></ul><div class="form-actions"><button class="button secondary" @click="reactionDetails=null">閉じる</button></div></section></div>
  <div v-if="readDetails" class="modal-backdrop" @click.self="readDetails=null"><section class="modal-card compact-modal"><h2>既読した人（{{ readDetails.actors.length }}人）</h2><ul class="chat-actor-list"><li v-for="actor in readDetails.actors" :key="actor">{{ actor }}</li></ul><div class="form-actions"><button class="button secondary" @click="readDetails=null">閉じる</button></div></section></div>
  <div v-if="lightboxItems.length" class="chat-lightbox" @click.self="closeLightbox" @keydown.esc="closeLightbox" @keydown.left="moveLightbox(-1)" @keydown.right="moveLightbox(1)" tabindex="-1"><button v-if="lightboxItems.length>1" type="button" class="chat-lightbox-prev" aria-label="前の画像" @click="moveLightbox(-1)">‹</button><img :src="lightboxItems[lightboxIndex].url" alt="送信画像を拡大表示"><button v-if="lightboxItems.length>1" type="button" class="chat-lightbox-next" aria-label="次の画像" @click="moveLightbox(1)">›</button><button type="button" aria-label="閉じる" @click="closeLightbox">×</button></div>
</template>
