import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
const mocks=vi.hoisted(()=>({counts:vi.fn(),refreshEvents:vi.fn(),events:new Map<string,()=>void>(),resume:()=>{},connection:(_value:boolean)=>{}}));
vi.mock('@/api/client',()=>({portalApi:{eventChatUnreadCounts:mocks.counts}}));
vi.mock('vue-router',()=>({useRouter:()=>({push:vi.fn(),resolve:()=>({href:'/'})})}));
vi.mock('@/stores/portal',()=>({usePortalStore:()=>({events:[{id:1,uuid:'test',title:'テスト',startsAt:'2099-01-01',permissions:{canOpenChat:true,canOpenAlbum:false},urls:{}}],refreshEvents:mocks.refreshEvents})}));
vi.mock('@/services/portalRealtime',()=>({
  onPortalEvent:(name:string,fn:()=>void)=>{mocks.events.set(name,fn);return ()=>mocks.events.delete(name);},
  onPortalResume:(fn:()=>void)=>{mocks.resume=fn;return ()=>{};},
  onPortalConnection:(fn:(value:boolean)=>void)=>{mocks.connection=fn;return ()=>{};},
}));
import EventListView from './EventListView.vue';
beforeEach(()=>{vi.useFakeTimers();vi.clearAllMocks();Object.defineProperty(document,'hidden',{configurable:true,value:false});mocks.counts.mockResolvedValue({counts:{'1':2}});});
afterEach(()=>{vi.useRealTimers();});
describe('event-card notification counts',()=>{
  it('uses event notification counts initially and after socket updates',async()=>{
    const wrapper=mount(EventListView); await flushPromises();
    expect(mocks.counts).toHaveBeenCalledWith([1]);
    expect(wrapper.find('[aria-label="チャット（未読2件）"]').exists()).toBe(true);
    mocks.counts.mockResolvedValue({counts:{'1':5}}); mocks.events.get('notif_unread')?.();
    vi.advanceTimersByTime(310); await flushPromises();
    expect(wrapper.find('[aria-label="チャット（未読5件）"]').exists()).toBe(true);
    wrapper.unmount();
  });
  it('refreshes on resume and periodically, but not while hidden',async()=>{
    const wrapper=mount(EventListView); await flushPromises();
    mocks.counts.mockClear(); mocks.resume(); vi.advanceTimersByTime(310); await flushPromises();
    expect(mocks.counts).toHaveBeenCalledTimes(1);
    mocks.counts.mockClear(); Object.defineProperty(document,'hidden',{configurable:true,value:true});
    vi.advanceTimersByTime(31000); await flushPromises(); expect(mocks.counts).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
