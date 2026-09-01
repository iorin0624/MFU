import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/config',()=>({runtimeConfig:{albumApiBase:'/album/api'}}));
import { setCsrfToken, uploadMediaWithProgress } from './client';
class Xhr {
  static last:Xhr;
  headers:Record<string,string>={};upload:any={};status=200;responseText='{"ok":true}';withCredentials=false;timeout=0;
  onload:()=>Promise<void>=async()=>{};onerror:()=>void=()=>{};onabort:()=>void=()=>{};
  open=vi.fn();send=vi.fn();abort=()=>this.onabort();
  constructor(){Xhr.last=this;}
  setRequestHeader(k:string,v:string){this.headers[k]=v;}
  getResponseHeader(){return 'application/json';}
}
afterEach(()=>{vi.unstubAllGlobals();setCsrfToken('');});
describe('upload progress transport',()=>{
  it('preserves CSRF/session and waits for successful JSON response',async()=>{
    vi.stubGlobal('XMLHttpRequest',Xhr);setCsrfToken('csrf-test');const progress=vi.fn();let finished=false;
    const pending=uploadMediaWithProgress('a','b',[new File(['x'],'one.jpg')],progress).then(()=>finished=true);
    const xhr=Xhr.last;expect(xhr.headers['X-CSRF-Token']).toBe('csrf-test');expect(xhr.withCredentials).toBe(true);expect((xhr.send.mock.calls[0][0] as FormData).getAll('file')).toHaveLength(1);
    xhr.upload.onprogress({lengthComputable:true,loaded:1,total:2});expect(progress).toHaveBeenCalledWith(.5);expect(finished).toBe(false);await xhr.onload();await pending;expect(finished).toBe(true);
  });
  it('rejects backend permission errors instead of reporting completion',async()=>{vi.stubGlobal('XMLHttpRequest',Xhr);const pending=uploadMediaWithProgress('a','b',[],vi.fn());const expectation=expect(pending).rejects.toThrow('権限がありません');Xhr.last.status=403;Xhr.last.responseText='{"ok":false,"message":"権限がありません"}';await Xhr.last.onload();await expectation;});
  it('aborts when component is disposed',async()=>{vi.stubGlobal('XMLHttpRequest',Xhr);const controller=new AbortController();const pending=uploadMediaWithProgress('a','b',[],vi.fn(),controller.signal);const expectation=expect(pending).rejects.toThrow('送信を中止');controller.abort();await expectation;});
});
