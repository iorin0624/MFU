import { defineStore } from 'pinia';
import { portalApi, setChatCsrfToken } from '@/api/client';
import { emitPortalEvent, onPortalConnection, onPortalEvent, onPortalResume, portalSocket } from '@/services/portalRealtime';
import { usePortalStore } from '@/stores/portal';
import { canonicalChatActorKey, mergeChatReadStates, presentChatMessage } from '@/utils/chatPresentation';
import { reconcileChatMessages } from '@/utils/chatRecovery';
import type { ChatBootstrap, ChatDmSummary, ChatEventSummary, ChatMessage, ChatReadState, ChatRoom } from '@/types';

function sortMessages(messages: ChatMessage[]) {
  return [...messages].sort((a, b) => Number(a.id) - Number(b.id));
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    bootstrap: null as ChatBootstrap | null,
    events: [] as ChatEventSummary[],
    dms: [] as ChatDmSummary[],
    messages: [] as ChatMessage[],
    rooms: [] as ChatRoom[],
    readStates: [] as ChatReadState[],
    firstUnreadMessageId: 0,
    activeRoom: null as ChatRoom | null,
    currentEventId: 0,
    currentDmUuid: '',
    currentTitle: '',
    canManageRooms: false,
    connected: false,
    loading: false,
    sending: false,
    hasMore: true,
    error: '',
    typingNames: [] as string[],
    replyTo: null as ChatMessage | null,
    editing: null as ChatMessage | null,
    realtimeBound: false,
    disposers: [] as Array<() => void>,
    presenceClientId: `vue-${crypto.randomUUID()}`,
    presenceTimer: 0,
    recoveryTimer: 0,
    recoveryBusy: false,
    recoveryToken: 0,
    roomGeneration: 0,
    recoveryError: '',
    lastRecoveryAt: 0,
    lastDisconnectedAt: 0,
    indexRefreshTimer: 0,
    indexRefreshBusy: false,
    indexRefreshPending: false,
  }),
  getters: {
    mode: (state): 'event' | 'dm' | null => state.currentDmUuid ? 'dm' : state.currentEventId ? 'event' : null,
    oldestMessageId: (state) => state.messages.length ? Math.min(...state.messages.map((item) => Number(item.id))) : 0,
    latestMessageId: (state) => state.messages.length ? Math.max(...state.messages.map((item) => Number(item.id))) : 0,
  },
  actions: {
    bindRealtime() {
      if (this.realtimeBound) return;
      this.realtimeBound = true;
      this.connected = Boolean(portalSocket()?.connected);
      this.disposers.push(onPortalConnection((connected) => {
        this.connected = connected;
        if (connected) {
          this.joinCurrent();
          if (this.currentEventId || this.currentDmUuid) void this.recoverCurrent();
          else this.queueIndexRefresh(0);
        }
        else {
          this.lastDisconnectedAt = Date.now();
          this.typingNames = [];
          void this.recoverCurrent();
        }
      }));
      this.disposers.push(onPortalResume(() => {
        this.joinCurrent();
        if (this.currentEventId || this.currentDmUuid) void this.recoverCurrent();
        else this.queueIndexRefresh(0);
      }));
      this.recoveryTimer = window.setInterval(() => {
        if (this.currentEventId || this.currentDmUuid) void this.recoverCurrent();
        else this.queueIndexRefresh(0);
      }, 30000);
      this.disposers.push(onPortalEvent('chat_message', (payload: ChatMessage) => {
        if (!this.matches(payload)) return;
        payload = presentChatMessage(payload, this.bootstrap?.actor, true);
        const index = this.messages.findIndex((item) => Number(item.id) === Number(payload.id));
        if (index >= 0) this.messages[index] = payload;
        else this.messages = sortMessages([...this.messages, payload]);
        this.markSeen();
      }));
      this.disposers.push(onPortalEvent('notif_unread', () => {
        if (this.currentEventId) void this.refreshRoomUnread();
        else if (!this.currentDmUuid) this.queueIndexRefresh();
      }));
      this.disposers.push(onPortalEvent('chat_edit_update', (payload: ChatMessage) => {
        if (!this.matches(payload)) return;
        this.replaceMessage(payload);
      }));
      this.disposers.push(onPortalEvent('chat_delete_update', (payload: any) => {
        if (!this.matches(payload)) return;
        const message = this.messages.find((item) => Number(item.id) === Number(payload.message_id));
        const notice=String(payload.deleted_text || (payload.deleted_by_actor_type === 'admin' ? '管理者により、削除されました' : 'このメッセージは削除されました'));
        for (const reply of this.messages) {
          if (Number(reply.reply_to_message_id) === Number(payload.message_id)) reply.reply_to_body_plain_excerpt=notice;
        }
        if (message) {
          Object.assign(message, payload, { body:'', body_plain:notice, editable_text:'', body_html:'', deleted_text:notice, has_image:false, images:[], image_url:null, image_thumb_url:null, reactions_summary:[], my_reaction:null, can_edit:false, can_delete:false, can_admin_delete:false });
        }
      }));
      this.disposers.push(onPortalEvent('chat_reaction_update', (payload: any) => {
        if (!this.matches(payload)) return;
        const message = this.messages.find((item) => Number(item.id) === Number(payload.message_id));
        if (message) message.reactions_summary = payload.reactions || [];
      }));
      this.disposers.push(onPortalEvent('chat_typing_update', (payload: any) => {
        if (!this.matches(payload)) return;
        const actorType = String(payload?.actor_type || payload?.actor?.actor_type || '');
        const actorId = String(payload?.actor_id || payload?.actor?.actor_id || '');
        const ownActorType = String(this.bootstrap?.actor.actor_type || '');
        const ownActorId = String(this.bootstrap?.actor.actor_id || '');
        if (actorType && actorId && actorType === ownActorType && actorId === ownActorId) return;
        const name = String(payload?.display_name || payload?.actor?.display_name || '');
        if (!name) return;
        const names = new Set(this.typingNames);
        payload.is_typing ? names.add(name) : names.delete(name);
        this.typingNames = [...names];
      }));
      this.disposers.push(onPortalEvent('chat_read_snapshot', (payload: any) => {
        if (this.matches(payload)) this.readStates = payload.read_states || [];
      }));
      this.disposers.push(onPortalEvent('chat_read_update', (payload: ChatReadState & Record<string, any>) => {
        if (!this.matches(payload)) return;
        this.readStates=mergeChatReadStates([...this.readStates,payload]);
      }));
      this.disposers.push(onPortalEvent('chat_error', (payload: any) => {
        this.error = String(payload?.error || 'チャット処理に失敗しました。');
      }));
    },
    unbindRealtime() {
      window.clearInterval(this.recoveryTimer);
      window.clearTimeout(this.indexRefreshTimer);
      this.indexRefreshTimer = 0;
      this.disposers.forEach((dispose) => dispose());
      this.disposers = [];
      this.realtimeBound = false;
    },
    matches(payload: any) {
      if (!this.currentEventId && !this.currentDmUuid) return false;
      if (this.currentDmUuid) return String(payload?.dm_uuid || '') === this.currentDmUuid;
      return Number(payload?.event_id || 0) === this.currentEventId
        && (!payload?.room_id || String(payload.room_id) === String(this.activeRoom?.room_id || ''));
    },
    replaceMessage(payload: ChatMessage) {
      const index = this.messages.findIndex((item) => Number(item.id) === Number(payload.id || (payload as any).message_id));
      if (index >= 0) this.messages[index] = presentChatMessage({ ...this.messages[index], ...payload }, this.bootstrap?.actor, true);
    },
    async loadBootstrap(force = false) {
      if (this.bootstrap && !force) return;
      this.loading = true; this.error = '';
      try {
        const response = await portalApi.chatBootstrap();
        this.bootstrap = response;
        this.events = response.accessible_events || [];
        this.dms = response.dm_inbox || [];
        setChatCsrfToken(response.csrf_token);
        this.bindRealtime();
      } catch (reason) {
        this.error = reason instanceof Error ? reason.message : 'チャット情報を取得できませんでした。';
      } finally { this.loading = false; }
    },
    queueIndexRefresh(delay = 200) {
      if (this.currentEventId || this.currentDmUuid || this.indexRefreshTimer) return;
      this.indexRefreshTimer = window.setTimeout(() => {
        this.indexRefreshTimer = 0;
        void this.refreshIndex();
      }, delay);
    },
    async refreshIndex() {
      if (this.currentEventId || this.currentDmUuid) return;
      if (this.indexRefreshBusy) {
        this.indexRefreshPending = true;
        return;
      }
      this.indexRefreshBusy = true;
      try {
        const response = await portalApi.chatBootstrap();
        if (this.currentEventId || this.currentDmUuid) return;
        this.bootstrap = response;
        this.events = response.accessible_events || [];
        this.dms = response.dm_inbox || [];
        setChatCsrfToken(response.csrf_token);
      } catch {
        // WebSocket切断時は次の再接続・画面復帰・定期取得で再試行する。
      } finally {
        this.indexRefreshBusy = false;
        if (this.indexRefreshPending) {
          this.indexRefreshPending = false;
          this.queueIndexRefresh(0);
        }
      }
    },
    resetRoom() {
      this.roomGeneration++;
      this.recoveryToken++;
      this.recoveryBusy = false;
      this.recoveryError = '';
      void this.leavePresence();
      this.messages = []; this.rooms = []; this.activeRoom = null;
      this.currentEventId = 0; this.currentDmUuid = ''; this.currentTitle = '';
      this.hasMore = true; this.typingNames = []; this.readStates=[]; this.firstUnreadMessageId=0; this.replyTo = null; this.editing = null; this.error = '';
    },
    async openEvent(eventId: number, roomId = '') {
      const generation = ++this.roomGeneration;
      this.loading = true; this.error = '';
      this.typingNames = [];
      try {
        await this.loadBootstrap();
        const response = await portalApi.chatEventSnapshot(eventId, roomId);
        if (generation !== this.roomGeneration) return;
        this.currentEventId = eventId; this.currentDmUuid = '';
        this.currentTitle = response.event.title;
        this.rooms = response.accessible_rooms || [];
        this.activeRoom = response.active_room;
        this.canManageRooms = response.can_manage_rooms;
        this.messages = sortMessages((response.messages || []).map((message) => presentChatMessage(message, this.bootstrap?.actor)));
        this.readStates = response.read_states || [];
        this.captureUnreadBoundary();
        this.hasMore = this.messages.length >= 100;
        setChatCsrfToken(response.csrf_token);
        this.bindRealtime(); this.joinCurrent(); this.markSeen();
        this.lastRecoveryAt = Date.now();
        this.recoveryError = '';
        await Promise.all([this.refreshRoomUnread(), this.markCurrentNotificationsRead()]);
        void this.enterPresence();
      } catch (reason) { this.error = reason instanceof Error ? reason.message : 'チャットを開けませんでした。'; }
      finally { this.loading = false; }
    },
    async openDm(dmUuid: string) {
      const generation = ++this.roomGeneration;
      this.loading = true; this.error = '';
      this.typingNames = [];
      try {
        await this.loadBootstrap();
        const response = await portalApi.chatDmSnapshot(dmUuid);
        if (generation !== this.roomGeneration) return;
        this.currentDmUuid = dmUuid; this.currentEventId = 0;
        this.currentTitle = response.peer_display_name || '個別メッセージ';
        this.messages = sortMessages((response.messages || []).map((message) => presentChatMessage(message, this.bootstrap?.actor)));
        this.readStates = response.read_states || [];
        this.captureUnreadBoundary();
        this.hasMore = this.messages.length >= 200;
        setChatCsrfToken(response.csrf_token);
        this.bindRealtime(); this.joinCurrent(); this.markSeen();
        this.lastRecoveryAt = Date.now();
        this.recoveryError = '';
        const dm = this.dms.find((item) => item.dm_uuid === dmUuid);
        if (dm) dm.unread_count = 0;
        await this.markCurrentNotificationsRead();
      } catch (reason) { this.error = reason instanceof Error ? reason.message : 'DMを開けませんでした。'; }
      finally { this.loading = false; }
    },
    joinCurrent() {
      if (this.currentDmUuid) emitPortalEvent('chat_join', { dm_uuid: this.currentDmUuid });
      else if (this.currentEventId && this.activeRoom) emitPortalEvent('chat_join', { event_id: this.currentEventId, room_id: this.activeRoom.room_id });
    },
    async recoverCurrent() {
      if (document.hidden || this.loading || this.recoveryBusy || (!this.currentEventId && !this.currentDmUuid)) return;
      const generation = this.roomGeneration;
      const eventId = this.currentEventId;
      const dmUuid = this.currentDmUuid;
      const roomId = this.activeRoom?.room_id || '';
      const token = ++this.recoveryToken;
      this.recoveryBusy = true;
      const baseline = new Map(this.messages.map((message) => [Number(message.id), JSON.stringify(message)]));
      try {
        this.joinCurrent();
        const response = dmUuid
          ? await portalApi.chatDmSnapshot(dmUuid)
          : await portalApi.chatEventSnapshot(eventId, roomId);
        if (generation !== this.roomGeneration || token !== this.recoveryToken) return;
        const snapshot = (response.messages || []).map((message) => presentChatMessage(message, this.bootstrap?.actor));
        const result = reconcileChatMessages(this.messages, snapshot, baseline);
        this.messages = result.messages;
        if (result.hasGap) this.hasMore = true;
        this.readStates = response.read_states || [];
        if ('accessible_rooms' in response) {
          this.rooms = response.accessible_rooms || [];
          this.activeRoom = response.active_room;
          this.canManageRooms = response.can_manage_rooms;
        }
        setChatCsrfToken(response.csrf_token);
        this.lastRecoveryAt = Date.now();
        this.recoveryError = '';
        if (!document.hidden) {
          this.markSeen();
          await this.markCurrentNotificationsRead();
          if (generation === this.roomGeneration && eventId) await this.refreshRoomUnread();
        }
      } catch {
        if (generation === this.roomGeneration) this.recoveryError = '最新メッセージを再取得できませんでした。接続回復後に再試行します。';
      } finally {
        if (token === this.recoveryToken) this.recoveryBusy = false;
      }
    },
    markSeen() {
      if (document.hidden || !portalSocket()?.connected || !this.latestMessageId) return;
      if (this.currentDmUuid) emitPortalEvent('chat_seen', { dm_uuid: this.currentDmUuid, last_seen_message_id: this.latestMessageId });
      else if (this.currentEventId && this.activeRoom) {
        emitPortalEvent('chat_seen', { event_id: this.currentEventId, room_id: this.activeRoom.room_id, last_seen_message_id: this.latestMessageId });
        const room = this.rooms.find((item) => item.room_id === this.activeRoom?.room_id);
        if (room) room.unread_count = 0;
      }
    },
    async refreshRoomUnread() {
      if (!this.currentEventId) return;
      try {
        const response = await portalApi.chatRoomUnread(this.currentEventId);
        const counts = new Map((response.rooms || []).map((item) => [String(item.room_id), item]));
        this.rooms = this.rooms.map((room) => ({ ...room, unread_count: Number(counts.get(String(room.room_id))?.unread_count || 0) }));
        if (this.activeRoom && !document.hidden) {
          const active = this.rooms.find((room) => room.room_id === this.activeRoom?.room_id);
          if (active) active.unread_count = 0;
        }
        const event = this.events.find((item) => Number(item.id) === this.currentEventId);
        if (event) event.unread_count = this.rooms.reduce((total, room) => total + Number(room.unread_count || 0), 0);
      } catch { /* 通信断時は直前の未読数を維持する */ }
    },
    async markCurrentNotificationsRead() {
      if (document.hidden) return;
      const scope = this.bootstrap?.notification_context?.notification_scope || 'external';
      try {
        if (this.currentDmUuid) {
          await portalApi.markDmRoomNotificationsRead(scope, this.currentDmUuid);
        } else if (this.currentEventId && this.activeRoom) {
          await portalApi.markChatRoomNotificationsRead(scope, this.currentEventId, this.activeRoom.room_id);
        } else return;
        await usePortalStore().refreshUnread();
      } catch {
        // 通知既読化に失敗しても、チャット本体の閲覧は継続できるようにする。
      }
    },
    setTyping(value: boolean) {
      if (this.currentEventId && this.activeRoom) emitPortalEvent('chat_typing', { event_id: this.currentEventId, room_id: this.activeRoom.room_id, is_typing: value });
    },
    async send(body: string) {
      const value = body.trim();
      if (!value) return;
      this.sending = true; this.error = '';
      try {
        if (this.currentDmUuid) emitPortalEvent('chat_send', { dm_uuid: this.currentDmUuid, body: value });
        else if (this.currentEventId && this.activeRoom) emitPortalEvent('chat_send', {
          event_id: this.currentEventId, room_id: this.activeRoom.room_id, body: value,
          reply_to_message_id: this.replyTo?.id || null,
          thread_root_id: this.replyTo?.thread_root_id || null,
        });
        this.replyTo = null;
      } finally { this.sending = false; }
    },
    sendThread(body: string, root: ChatMessage) {
      const value = body.trim();
      if (!value || !this.currentEventId || !this.activeRoom) return;
      emitPortalEvent('chat_send', {
        event_id: this.currentEventId,
        room_id: this.activeRoom.room_id,
        body: value,
        reply_to_message_id: root.id,
        thread_root_id: root.thread_root_id || root.id,
      });
    },
    react(message: ChatMessage, emoji: string) {
      if (this.currentDmUuid) emitPortalEvent('dm_react', { dm_uuid: this.currentDmUuid, message_id: message.id, emoji });
      else if (this.currentEventId && this.activeRoom) emitPortalEvent('chat_react', { event_id: this.currentEventId, room_id: this.activeRoom.room_id, message_id: message.id, emoji });
    },
    readersFor(messageId: number) {
      const actor=this.bootstrap?.actor;
      const myKey=canonicalChatActorKey(actor?.actor_key || (actor ? `${actor.actor_type}:${actor.actor_id}` : ''));
      return mergeChatReadStates(this.readStates).filter((item)=>item.actor_key!==myKey && Number(item.last_read_message_id)>=Number(messageId)).map((item)=>item.display_name);
    },
    captureUnreadBoundary() {
      const actor=this.bootstrap?.actor;
      const myKey=canonicalChatActorKey(actor?.actor_key || (actor ? `${actor.actor_type}:${actor.actor_id}` : ''));
      const own = mergeChatReadStates(this.readStates).find((item)=>item.actor_key===myKey);
      const last = Number(own?.last_read_message_id || 0);
      this.firstUnreadMessageId = Number(this.messages.find((item)=>Number(item.id)>last)?.id || 0);
    },
    consumeUnreadBoundary() {
      this.firstUnreadMessageId = 0;
    },
    async loadOlder() {
      if (this.loading || !this.hasMore || !this.oldestMessageId) return;
      this.loading = true;
      try {
        const response = this.currentDmUuid
          ? await portalApi.chatDmOlderMessages(this.currentDmUuid, this.oldestMessageId)
          : await portalApi.chatOlderMessages(this.currentEventId, String(this.activeRoom?.room_id || ''), this.oldestMessageId);
        const known = new Set(this.messages.map((message) => Number(message.id)));
        this.messages = sortMessages([...(response.messages || []).filter((message) => !known.has(Number(message.id))).map((message) => presentChatMessage(message, this.bootstrap?.actor)), ...this.messages]);
        this.hasMore = Boolean(response.has_more);
      } catch (reason) { this.error = reason instanceof Error ? reason.message : '過去のメッセージを取得できませんでした。'; }
      finally { this.loading = false; }
    },
    async enterPresence() {
      if (!this.currentEventId || !this.activeRoom) return;
      window.clearInterval(this.presenceTimer);
      try { await portalApi.chatPresence('enter', this.currentEventId, this.activeRoom.room_id, this.presenceClientId, !document.hidden); }
      catch { return; }
      this.presenceTimer = window.setInterval(() => {
        if (this.currentEventId && this.activeRoom) void portalApi.chatPresence('ping', this.currentEventId, this.activeRoom.room_id, this.presenceClientId, !document.hidden).catch(() => undefined);
      }, 30000);
    },
    async leavePresence() {
      window.clearInterval(this.presenceTimer); this.presenceTimer = 0;
      if (!this.currentEventId || !this.activeRoom) return;
      await portalApi.chatPresence('leave', this.currentEventId, this.activeRoom.room_id, this.presenceClientId).catch(() => undefined);
    },
  },
});
