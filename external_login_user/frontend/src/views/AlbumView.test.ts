import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
const mocks=vi.hoisted(()=>({create:vi.fn(),media:vi.fn()}));
const child=(id:string,name:string)=>({id,name,mode:'normal',mediaCount:0,permissions:{},mediaUnit:'枚'});
vi.mock('@/stores/portal',()=>({usePortalStore:()=>({session:{profile:{nickname:'Aさん'}}})}));
vi.mock('@/utils/inAppBrowser',()=>({isInAppBrowser:()=>false}));
vi.mock('vue-router',()=>({useRoute:()=>({params:{albumId:'a'},query:{}}),useRouter:()=>({push:vi.fn(),replace:vi.fn()}),onBeforeRouteLeave:vi.fn()}));
vi.mock('@/api/client',()=>({ApiError:class extends Error{},portalApi:{album:async()=>({album:{name:'test',permissions:{canCreateChild:true}}}),children:async()=>({children:[child('1','【構図】Aさん'),child('2','【オフショ】Bさん')]}),media:mocks.media,createChild:mocks.create}}));
import AlbumView from './AlbumView.vue';
beforeEach(()=>{vi.stubGlobal('matchMedia',()=>({matches:true,addEventListener:vi.fn(),removeEventListener:vi.fn()}));vi.stubGlobal('IntersectionObserver',class{observe(){}disconnect(){}});mocks.create.mockResolvedValue({child:child('3','【動画】Aさん')});mocks.media.mockResolvedValue({child:child('3','【動画】Aさん'),media:[],pagination:{total:0,hasNext:false}});});
afterEach(()=>vi.unstubAllGlobals());
describe('album legacy creation helpers',()=>{
  it('requires a template and locks its type for participants',async()=>{const w=mount(AlbumView);await flushPromises();await w.find('.sidebar-add').trigger('click');const selects=w.findAll('.modal-card select');expect(selects[0].findAll('option').map(o=>o.attributes('value'))).toEqual(['【構図】','【オフショ】','【動画】','【加工回し】']);expect(selects[1].attributes()).toHaveProperty('disabled');for(const [template,mode] of [['【構図】','normal'],['【オフショ】','normal'],['【動画】','movie'],['【加工回し】','process']]){await selects[0].setValue(template);expect((selects[1].element as HTMLSelectElement).value).toBe(mode);}w.unmount();});
  it('uses the current participant name and selected template',async()=>{const w=mount(AlbumView);await flushPromises();await w.find('.sidebar-add').trigger('click');expect((w.find('.modal-card input').element as HTMLInputElement).value).toBe('Aさん');await w.find('.modal-card select').setValue('【動画】');await w.find('.modal-card').trigger('submit');await flushPromises();expect(mocks.create).toHaveBeenCalledWith('a','【動画】Aさん','movie');w.unmount();});
  it('expands and collapses all groups',async()=>{const w=mount(AlbumView);await flushPromises();expect(w.findAll('details[open]')).toHaveLength(2);await w.findAll('.album-group-actions button')[1].trigger('click');expect(w.findAll('details[open]')).toHaveLength(0);await w.findAll('.album-group-actions button')[0].trigger('click');expect(w.findAll('details[open]')).toHaveLength(2);w.unmount();});
});
