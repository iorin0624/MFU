import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { reactive } from 'vue';
const mocks=vi.hoisted(()=>({threads:vi.fn(),edit:vi.fn(),remove:vi.fn(),handlers:new Map<string,Function>(),chat:null as any}));
vi.mock('@/api/client',()=>({portalApi:{chatThreads:mocks.threads,chatEditMessage:mocks.edit,chatDeleteMessage:mocks.remove}}));
vi.mock('@/stores/chat',()=>({useChatStore:()=>mocks.chat}));
vi.mock('@/services/portalRealtime',()=>({onPortalEvent:(name:string,handler:Function)=>{mocks.handlers.set(name,handler);return ()=>mocks.handlers.delete(name);},onPortalResume:()=>()=>{},onPortalConnection:()=>()=>{}}));
vi.mock('@/utils/chatComposer',()=>({resizeChatComposer:()=>78}));
import ChatRoomPane from './ChatRoomPane.vue';
const root={id:1,body_plain:'元の本文',sender_display_name:'Aさん',created_at_iso:'2026-01-01T00:00:00Z',reactions_summary:[],is_me:false};
beforeEach(()=>{
  vi.useFakeTimers();mocks.handlers.clear();mocks.threads.mockReset();
  mocks.threads.mockResolvedValue({root,replies:[]});
  mocks.chat=reactive({messages:[root],rooms:[],activeRoom:{room_id:'main',room_name:'main'},currentEventId:1,currentDmUuid:'',currentTitle:'test',bootstrap:{limits:{message_max_len:2000},reaction_emojis:[]},typingNames:[],setTyping:vi.fn(),matches:(p:any)=>p.event_id===1&&p.room_id==='main'});
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});
  vi.stubGlobal('requestAnimationFrame',()=>1);vi.stubGlobal('cancelAnimationFrame',()=>{});
  vi.stubGlobal('matchMedia',()=>({matches:false}));
});
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();});
async function openThread(w:ReturnType<typeof mount>){await w.find('article').trigger('contextmenu');await w.findAll('.chat-message-menu button').find(b=>b.text().startsWith('スレッド'))!.trigger('click');await vi.advanceTimersByTimeAsync(100);await flushPromises();}
describe('chat missing legacy features',()=>{
  it('places reactions and read details together in the message footer',async()=>{
    mocks.chat.messages[0]={...root,is_me:true,reactions_summary:[{emoji:'💕',count:1}]};
    mocks.chat.readersFor=()=>['Bさん'];
    const w=mount(ChatRoomPane);const footer=w.find('.chat-message-footer');
    expect(footer.find('.chat-reactions').text()).toContain('💕 1');
    expect(footer.find('.chat-readers').text()).toBe('既読 1');
    await footer.find('.chat-readers').trigger('click');
    expect(w.find('.chat-actor-list').text()).toBe('Bさん');w.unmount();
  });
  it('edits in the normal composer, preserves failures and restores the draft after saving',async()=>{
    mocks.chat.messages[0]={...root,can_edit:true};mocks.edit.mockReset();
    mocks.edit.mockRejectedValueOnce(new Error('送信から1時間を過ぎています'));
    const w=mount(ChatRoomPane);const area=w.find('.chat-composer textarea');
    await area.setValue('保存前の下書き');
    await w.find('article').trigger('contextmenu');
    await w.findAll('.chat-message-menu button').find(b=>b.text()==='編集')!.trigger('click');
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect((area.element as HTMLTextAreaElement).value).toBe('元の本文');
    await area.setValue('1行目\n2行目');await area.trigger('keydown',{key:'Enter'});
    expect(mocks.edit).not.toHaveBeenCalled();
    await w.find('.chat-composer').trigger('submit');await flushPromises();
    expect(mocks.edit).toHaveBeenCalledWith(1,'main',1,'1行目\n2行目');
    expect((area.element as HTMLTextAreaElement).value).toBe('1行目\n2行目');
    expect(w.find('[role="alert"]').text()).toContain('1時間');
    mocks.edit.mockResolvedValueOnce({ok:true});
    await w.find('.chat-composer').trigger('submit');await flushPromises();
    expect(w.find('[aria-label="編集を取り消す"]').exists()).toBe(false);
    expect((area.element as HTMLTextAreaElement).value).toBe('保存前の下書き');
    w.unmount();
  });
  it('cancels editing and restores the original draft and reply',async()=>{
    mocks.chat.messages[0]={...root,can_edit:true};const reply={...root,id:9};mocks.chat.replyTo=reply;
    const w=mount(ChatRoomPane);const area=w.find('.chat-composer textarea');await area.setValue('下書き');
    await w.find('article').trigger('contextmenu');await w.findAll('.chat-message-menu button').find(b=>b.text()==='編集')!.trigger('click');
    expect(mocks.chat.replyTo).toBeNull();expect(w.find('input[type="file"]').attributes()).toHaveProperty('disabled');
    await area.setValue('変更途中');await w.find('[aria-label="編集を取り消す"]').trigger('click');
    expect((area.element as HTMLTextAreaElement).value).toBe('下書き');expect(mocks.chat.replyTo.id).toBe(9);
    w.unmount();
  });
  it('does not send a new message or overwrite another room after an in-flight edit',async()=>{
    mocks.chat.messages[0]={...root,can_edit:true};mocks.chat.send=vi.fn();mocks.edit.mockReset();
    let resolve!:(value:any)=>void;mocks.edit.mockImplementation(()=>new Promise(r=>resolve=r));
    const w=mount(ChatRoomPane);await w.find('article').trigger('contextmenu');await w.findAll('.chat-message-menu button').find(b=>b.text()==='編集')!.trigger('click');
    await w.find('.chat-composer textarea').setValue('変更中');
    await w.find('.chat-composer').trigger('submit');await w.find('.chat-composer').trigger('submit');
    expect(mocks.edit).toHaveBeenCalledTimes(1);expect(mocks.chat.send).not.toHaveBeenCalled();
    mocks.chat.activeRoom={room_id:'other'};await flushPromises();
    await w.find('.chat-composer textarea').setValue('別ルームの下書き');resolve({ok:true});await flushPromises();
    expect((w.find('.chat-composer textarea').element as HTMLTextAreaElement).value).toBe('別ルームの下書き');
    expect(w.find('[aria-label="編集を取り消す"]').exists()).toBe(false);w.unmount();
  });
  it('distinguishes self cancellation and admin deletion',async()=>{mocks.chat.messages[0]={...root,can_delete:true,can_admin_delete:true};vi.spyOn(window,'confirm').mockReturnValue(true);mocks.remove.mockResolvedValue({ok:true});const w=mount(ChatRoomPane);for(const [label,mode] of [['送信取消','cancel'],['管理者として削除','admin']]){await w.find('article').trigger('contextmenu');await w.findAll('.chat-message-menu button').find(b=>b.text()===label)!.trigger('click');await flushPromises();expect(mocks.remove).toHaveBeenLastCalledWith(1,'main',1,mode);}w.unmount();vi.restoreAllMocks();});
  it('provides selectable plain text without rendering message markup',async()=>{const w=mount(ChatRoomPane);await w.find('article').trigger('contextmenu');await w.findAll('button').find(b=>b.text()==='選択コピー')!.trigger('click');expect((w.find('.selection-copy-modal textarea').element as HTMLTextAreaElement).value).toBe('元の本文');expect(w.find('.selection-copy-modal textarea').attributes()).toHaveProperty('readonly');w.unmount();});
  it('refreshes open thread for incoming, edited and deleted messages',async()=>{const w=mount(ChatRoomPane);await openThread(w);for(const [event,text] of [['chat_message','返信本文'],['chat_edit_update','編集本文'],['chat_delete_update','削除済み']]){mocks.threads.mockResolvedValue({root,replies:[{...root,id:2,body_plain:text}]});mocks.handlers.get(event)!({event_id:1,room_id:'main',message_id:2});await vi.advanceTimersByTimeAsync(100);await flushPromises();expect(w.find('.chat-thread-modal').text()).toContain(text);}expect(mocks.threads).toHaveBeenCalledTimes(4);w.unmount();expect(mocks.handlers.size).toBe(0);});
  it('ignores other rooms and stale response after closing',async()=>{const w=mount(ChatRoomPane);await openThread(w);mocks.handlers.get('chat_message')!({event_id:2,room_id:'other'});await vi.advanceTimersByTimeAsync(100);expect(mocks.threads).toHaveBeenCalledTimes(1);let resolve!:(value:any)=>void;mocks.threads.mockImplementation(()=>new Promise(r=>resolve=r));mocks.handlers.get('chat_message')!({event_id:1,room_id:'main'});await vi.advanceTimersByTimeAsync(100);await w.find('.chat-thread-modal .form-actions button').trigger('click');resolve({root,replies:[]});await flushPromises();expect(w.find('.chat-thread-modal').exists()).toBe(false);w.unmount();});
});
