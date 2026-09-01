import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import AvatarCropDialog from './AvatarCropDialog.vue';
beforeEach(()=>{vi.spyOn(URL,'createObjectURL').mockReturnValue('blob:crop');vi.spyOn(URL,'revokeObjectURL').mockImplementation(()=>{});});
afterEach(()=>vi.restoreAllMocks());
describe('avatar crop dialog',()=>{
  it('exports the selected square at 512px and cleans up preview',async()=>{
    const draw=vi.fn();vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({fillRect:vi.fn(),drawImage:draw} as any);
    vi.spyOn(HTMLCanvasElement.prototype,'toBlob').mockImplementation(callback=>callback(new Blob(['jpg'],{type:'image/jpeg'})));
    const w=mount(AvatarCropDialog,{props:{file:new File(['image'],'source.png',{type:'image/png'})}});
    const img=w.find('img');Object.defineProperty(img.element,'naturalWidth',{value:1000});Object.defineProperty(img.element,'naturalHeight',{value:2000});await img.trigger('load');
    await w.findAll('input')[0].setValue('2');await w.findAll('input')[1].setValue('1');await w.findAll('input')[2].setValue('-1');
    await w.findAll('button').find(b=>b.text()==='切り取り適用')!.trigger('click');await flushPromises();
    expect(draw.mock.calls[0].slice(1)).toEqual([500,0,500,500,0,0,512,512]);expect((w.emitted('apply')![0][0] as File).type).toBe('image/jpeg');w.unmount();expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:crop');
  });
  it('cancel does not change profile image',async()=>{const w=mount(AvatarCropDialog,{props:{file:new File(['x'],'source.png')}});await w.findAll('button').find(b=>b.text()==='キャンセル')!.trigger('click');expect(w.emitted('apply')).toBeUndefined();expect(w.emitted('close')).toHaveLength(1);w.unmount();});
});
