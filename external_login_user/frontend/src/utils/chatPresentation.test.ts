import { describe, expect, it } from 'vitest';
import { presentChatMessage, shouldSendOnKey } from './chatPresentation';
import type { ChatBootstrap, ChatMessage } from '@/types';

const actor = (type: string, id: string): ChatBootstrap['actor'] => ({ actor_type:type, actor_id:id, actor_key:`${type}:${id}`, display_name:'Aさん', is_chat_admin_alias:false });
const message = { id:1, sender_id:'line:100', is_me:true, can_edit:true, can_delete:true, created_at_iso:new Date().toISOString(), deleted_flag:0 } as ChatMessage;
describe('recipient-specific chat presentation', () => {
  it('limits own editing and cancellation to one hour, but not admin deletion', () => {
    const old={...message,created_at_iso:new Date(Date.now()-3600001).toISOString()};
    expect(presentChatMessage(old,actor('line','100'),true)).toMatchObject({can_edit:false,can_delete:false,can_admin_delete:false});
    expect(presentChatMessage(old,actor('admin','1'),true)).toMatchObject({can_edit:false,can_delete:false,can_admin_delete:true});
    expect(presentChatMessage({...old,deleted_flag:1},actor('admin','1'),true).can_admin_delete).toBe(false);
  });
  it('shows other senders on the left even when broadcast is_me is true', () => {
    const result = presentChatMessage(message, actor('line','200'), true);
    expect(result.is_me).toBe(false);
    expect(result.can_edit).toBe(false);
    expect(result.can_delete).toBe(false);
  });
  it('shows own messages on the right and distinguishes actor namespaces', () => {
    expect(presentChatMessage(message, actor('line','100'), true).is_me).toBe(true);
    expect(presentChatMessage(message, actor('acl','100'), true).is_me).toBe(false);
    expect(presentChatMessage(message, undefined, true).is_me).toBe(false);
  });
  it('normalizes the existing admin alias without aliasing test users', () => {
    const adminMessage = { ...message, sender_id:'admin:admin' };
    expect(presentChatMessage(adminMessage, actor('admin','1'), true).is_me).toBe(true);
    expect(presentChatMessage(adminMessage, actor('line','200'), true).is_me).toBe(false);
  });
  it('keeps snapshot permissions and expires broadcast editing', () => {
    expect(presentChatMessage({ ...message, can_edit:false }, actor('line','100')).can_edit).toBe(false);
    expect(presentChatMessage({ ...message, created_at_iso:'2020-01-01T00:00:00Z' }, actor('line','100'), true).can_edit).toBe(false);
  });
});
describe('composer keyboard', () => {
  it('uses Enter for newline on both mobile and desktop', () => {
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter'}), false)).toBe(false);
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter'}), true)).toBe(false);
  });
  it('sends only with desktop Alt+Enter, never during IME composition', () => {
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter',altKey:true}), false)).toBe(true);
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter',altKey:true}), true)).toBe(false);
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter',altKey:true,isComposing:true}), false)).toBe(false);
    expect(shouldSendOnKey(new KeyboardEvent('keydown',{key:'Enter',altKey:true,repeat:true}), false)).toBe(false);
  });
});
