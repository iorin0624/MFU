import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
const mocks=vi.hoisted(()=>({snapshot:vi.fn(),dmSnapshot:vi.fn(),bootstrap:vi.fn(),emit:vi.fn(),read:vi.fn(),unread:vi.fn(),leave:vi.fn(),handlers:{} as Record<string,(payload?:any)=>void>,socket:{connected:true}}));
vi.mock('@/api/client',()=>({setChatCsrfToken:vi.fn(),portalApi:{chatBootstrap:mocks.bootstrap,chatEventSnapshot:mocks.snapshot,chatDmSnapshot:mocks.dmSnapshot,markChatRoomNotificationsRead:mocks.read,chatRoomUnread:mocks.unread,chatPresence:mocks.leave}}));
vi.mock('@/services/portalRealtime',()=>({emitPortalEvent:mocks.emit,portalSocket:()=>mocks.socket,onPortalConnection:()=>()=>{},onPortalEvent:(name:string,handler:(payload?:any)=>void)=>{mocks.handlers[name]=handler;return()=>{delete mocks.handlers[name];};},onPortalResume:()=>()=>{}}));
vi.mock('@/stores/portal',()=>({usePortalStore:()=>({refreshUnread:vi.fn()})}));
import { useChatStore } from './chat';

const message=(id:number)=>({id,sender_id:'line:2',created_at_iso:new Date().toISOString(),body_plain:'Aさん'});
const response=()=>({messages:[message(1),message(2)],read_states:[],accessible_rooms:[{room_id:'main'}],active_room:{room_id:'main'},can_manage_rooms:false,csrf_token:'test'});
beforeEach(()=>{
  setActivePinia(createPinia()); vi.clearAllMocks(); Object.keys(mocks.handlers).forEach((key)=>delete mocks.handlers[key]);
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  mocks.snapshot.mockResolvedValue(response()); mocks.read.mockResolvedValue({}); mocks.unread.mockResolvedValue({rooms:[]}); mocks.leave.mockResolvedValue({});
  mocks.bootstrap.mockResolvedValue({actor:{actor_type:'line',actor_id:'1'},csrf_token:'next',accessible_events:[],dm_inbox:[]} as any);
});
function activeStore() {
  const chat=useChatStore(); chat.currentEventId=1; chat.activeRoom={room_id:'main',room_name:'メイン',is_main:true};
  chat.bootstrap={actor:{actor_type:'line',actor_id:'1',actor_key:'line:1'}} as any;
  chat.messages=[message(1)] as any;
  return chat;
}
describe('chat resume synchronization',()=>{
  it('refreshes event and DM badges when an unread event arrives on the chat index',async()=>{
    vi.useFakeTimers();
    try {
      const chat=useChatStore();
      chat.bootstrap={actor:{actor_type:'line',actor_id:'1'}} as any;
      chat.bindRealtime();
      mocks.bootstrap.mockResolvedValue({
        actor:{actor_type:'line',actor_id:'1'},csrf_token:'next',
        accessible_events:[{id:10,title:'テスト',unread_count:2}],
        dm_inbox:[{dm_uuid:'dm-1',peer_actor_key:'line:2',unread_count:1}],
      } as any);

      mocks.handlers.notif_unread?.({chat:3});
      await vi.advanceTimersByTimeAsync(200);

      expect(mocks.bootstrap).toHaveBeenCalledOnce();
      expect(chat.events[0]?.unread_count).toBe(2);
      expect(chat.dms[0]?.unread_count).toBe(1);
      chat.unbindRealtime();
    } finally { vi.useRealTimers(); }
  });
  it('excludes the current admin alias from read counts and reader names',()=>{
    const chat=activeStore();
    chat.bootstrap!.actor={actor_type:'admin',actor_id:'admin',actor_key:'admin:1',display_name:'Aさん',is_chat_admin_alias:true};
    chat.readStates=[{actor_key:'admin:admin',display_name:'Aさん',last_read_message_id:5},{actor_key:'admin:1',display_name:'Aさん',last_read_message_id:4},{actor_key:'line:2',display_name:'Bさん',last_read_message_id:5}];
    expect(chat.readersFor(5)).toEqual(['Bさん']);
    expect(chat.readersFor(6)).toEqual([]);
  });
  it('counts admin spellings once without merging separate users with the same display name',()=>{
    const chat=activeStore();
    chat.readStates=[{actor_key:'line:1',display_name:'Aさん',last_read_message_id:5},{actor_key:'admin',display_name:'Bさん',last_read_message_id:5},{actor_key:'admin:admin',display_name:'Bさん',last_read_message_id:5},{actor_key:'admin:1',display_name:'Bさん',last_read_message_id:3},{actor_key:'line:2',display_name:'Bさん',last_read_message_id:5}];
    expect(chat.readersFor(5)).toEqual(['Bさん','Bさん']);
  });
  it('uses the latest canonical own read state for the initial unread boundary',()=>{
    const chat=activeStore();
    chat.bootstrap!.actor={actor_type:'admin',actor_id:'admin',actor_key:'admin:1',display_name:'Aさん',is_chat_admin_alias:true};
    chat.messages=[message(3),message(4),message(5)] as any;
    chat.readStates=[{actor_key:'admin:admin',display_name:'Aさん',last_read_message_id:4},{actor_key:'admin:1',display_name:'Aさん',last_read_message_id:2}];
    chat.captureUnreadBoundary();expect(chat.firstUnreadMessageId).toBe(5);
  });
  it('fetches missed messages without a socket disconnect',async()=>{
    const chat=activeStore(); await chat.recoverCurrent();
    expect(chat.messages.map(m=>m.id)).toEqual([1,2]);
    expect(mocks.snapshot).toHaveBeenCalledWith(1,'main');
  });
  it('does not read or reconcile messages while hidden',async()=>{
    const chat=activeStore(); Object.defineProperty(document,'hidden',{configurable:true,value:true});
    chat.markSeen(); await chat.recoverCurrent(); await chat.markCurrentNotificationsRead();
    expect(mocks.emit).not.toHaveBeenCalled(); expect(mocks.read).not.toHaveBeenCalled(); expect(mocks.snapshot).not.toHaveBeenCalled();
  });
  it('ignores a recovery response after leaving the chat room',async()=>{
    let resolve!:(value:any)=>void; mocks.snapshot.mockImplementation(()=>new Promise(r=>{resolve=r;}));
    const chat=activeStore(); const pending=chat.recoverCurrent(); chat.resetRoom(); resolve(response()); await pending;
    expect(chat.currentEventId).toBe(0); expect(chat.messages).toEqual([]); expect(mocks.read).not.toHaveBeenCalled();
  });
  it('coalesces concurrent recovery requests',async()=>{
    let resolve!:(value:any)=>void; mocks.snapshot.mockImplementation(()=>new Promise(r=>{resolve=r;}));
    const chat=activeStore(); const pending=chat.recoverCurrent(); await chat.recoverCurrent();
    expect(mocks.snapshot).toHaveBeenCalledTimes(1); resolve(response()); await pending;
  });
});
