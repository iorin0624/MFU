import { describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
vi.mock('@/api/client',()=>({portalApi:{chatRoomMemberCandidates:vi.fn(async()=>({candidates:[{actor_key:'line:1',display_name:'Aさん'},{actor_key:'line:2',display_name:'Bさん'}]}))}}));
import ChatRoomManager from './ChatRoomManager.vue';
describe('room member selection',()=>{it('selects and clears all candidates',async()=>{const w=mount(ChatRoomManager,{props:{eventId:1,rooms:[]}});await flushPromises();await w.findAll('button').find(b=>b.text()==='全選択')!.trigger('click');expect(w.findAll('input:checked')).toHaveLength(2);await w.findAll('button').find(b=>b.text()==='全解除')!.trigger('click');expect(w.findAll('input:checked')).toHaveLength(0);w.unmount();});});
describe('room member search',()=>{it('filters candidates by display name',async()=>{const w=mount(ChatRoomManager,{props:{eventId:1,rooms:[]}});await flushPromises();await w.get('#chat-room-member-search').setValue('B');expect(w.findAll('label.check-row')).toHaveLength(1);expect(w.text()).toContain('Bさん');expect(w.text()).not.toContain('Aさん');w.unmount();});});
