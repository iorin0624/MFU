import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
const mocks=vi.hoisted(()=>({upload:vi.fn()}));
vi.mock('@/api/client',()=>({uploadMediaWithProgress:mocks.upload}));
vi.mock('vue-router',()=>({onBeforeRouteLeave:vi.fn()}));
import AlbumUploadDialog from './AlbumUploadDialog.vue';
beforeEach(()=>{mocks.upload.mockReset();vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:vi.fn(()=>'blob:preview'),revokeObjectURL:vi.fn()}));});
afterEach(()=>vi.unstubAllGlobals());
const files=()=>Array.from({length:81},(_,i)=>new File(['data'],`${i}.jpg`,{type:'image/jpeg'}));
describe('album upload confirmation',()=>{
  it('previews first, sends sequential batches, shows final progress',async()=>{
    mocks.upload.mockImplementation(async(_a,_c,_files,progress)=>progress(1));
    const wrapper=mount(AlbumUploadDialog,{props:{albumId:'a',childId:'b',childName:'test',files:files()}});
    expect(mocks.upload).not.toHaveBeenCalled();expect(wrapper.findAll('figure')).toHaveLength(40);
    await wrapper.findAll('button').find(b=>b.text()==='アップロード開始')!.trigger('click');await flushPromises();
    expect(mocks.upload.mock.calls.map(c=>c[2].length)).toEqual([80,1]);expect(wrapper.text()).toContain('100%');expect(wrapper.emitted('completed')).toEqual([['b']]);wrapper.unmount();
  });
  it('stops on failure and does not resend saved batches',async()=>{
    mocks.upload.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('通信失敗'));
    const wrapper=mount(AlbumUploadDialog,{props:{albumId:'a',childId:'b',childName:'test',files:files()}});
    await wrapper.findAll('button').find(b=>b.text()==='アップロード開始')!.trigger('click');await flushPromises();
    expect(mocks.upload).toHaveBeenCalledTimes(2);expect(wrapper.text()).toContain('80ファイル保存確認済み');expect(wrapper.text()).toContain('通信失敗');expect(wrapper.text()).not.toContain('アップロード開始');wrapper.unmount();
  });
});
