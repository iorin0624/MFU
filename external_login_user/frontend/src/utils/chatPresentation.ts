import type { ChatBootstrap, ChatMessage, ChatReadState } from '@/types';

export function canonicalChatActorKey(value: string) {
  value = String(value || '').trim();
  return value === 'admin' || value.startsWith('admin:') ? 'admin:1' : value;
}

export function mergeChatReadStates(states: ChatReadState[]): ChatReadState[] {
  const merged=new Map<string,ChatReadState>();
  for (const state of states) {
    const key=canonicalChatActorKey(state.actor_key);
    if (!key) continue;
    const previous=merged.get(key);
    if (!previous || Number(state.last_read_message_id)>=Number(previous.last_read_message_id)) {
      merged.set(key,{...state,actor_key:key});
    }
  }
  return [...merged.values()];
}

// Room broadcasts carry the sender's is_me/permission flags, not the viewer's.
export function presentChatMessage(message: ChatMessage, actor: ChatBootstrap['actor'] | undefined, broadcast = false): ChatMessage {
  const ownKey = actor ? canonicalChatActorKey(actor.actor_key || `${actor.actor_type}:${actor.actor_id}`) : '';
  const mine = Boolean(ownKey && message.sender_id && canonicalChatActorKey(message.sender_id) === ownKey);
  if (!broadcast) return { ...message, is_me: mine };
  const age = Date.now() - Date.parse(message.created_at_iso);
  const editable = mine && !message.deleted_flag && Number.isFinite(age) && age >= 0 && age <= 60 * 60 * 1000;
  return { ...message, is_me: mine, can_edit: editable,
    can_delete: editable,
    can_admin_delete: !message.deleted_flag && actor?.actor_type === 'admin',
    my_reaction: null };
}

export function shouldSendOnKey(event: KeyboardEvent, mobile: boolean) {
  return !mobile && event.key === 'Enter' && event.altKey && !event.ctrlKey && !event.metaKey
    && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !event.repeat;
}
