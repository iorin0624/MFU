import { describe, expect, it } from 'vitest';
import { reconcileChatMessages } from './chatRecovery';
import type { ChatMessage } from '@/types';
const message=(id:number,body='Aさん')=>({id,body_plain:body} as ChatMessage);
const baseline=(messages:ChatMessage[])=>new Map(messages.map(m=>[m.id,JSON.stringify(m)]));
describe('chat recovery snapshots', () => {
  it('deduplicates overlapping pages and adds missed messages', () => {
    const old=[message(1),message(2)];
    expect(reconcileChatMessages(old,[message(2),message(3)],baseline(old)).messages.map(m=>m.id)).toEqual([1,2,3]);
  });
  it('does not hide a gap after more than a page of missed messages', () => {
    const old=[message(1)];
    const result=reconcileChatMessages(old,[message(150),message(151)],baseline(old));
    expect(result.hasGap).toBe(true);
    expect(result.messages.map(m=>m.id)).toEqual([150,151]);
  });
  it('preserves live messages and edits received while the HTTP request runs', () => {
    const old=[message(1)];
    const result=reconcileChatMessages([message(1,'Bさん'),message(3)],[message(1),message(2)],baseline(old));
    expect(result.messages.map(m=>m.id)).toEqual([1,2,3]);
    expect(result.messages[0].body_plain).toBe('Bさん');
  });
});
