import { describe, expect, it } from 'vitest';
import { buildUploadBatches, MAX_UPLOAD_BYTES, uploadPercent, childTemplateMode } from './albumUpload';
describe('album upload batching',()=>{
  it('splits at 80 files preserving order',()=>{
    const files=Array.from({length:161},(_,id)=>({id,size:1}));
    const batches=buildUploadBatches(files);
    expect(batches.map(b=>b.length)).toEqual([80,80,1]);expect(batches.flat()).toEqual(files);
  });
  it('splits at 350MB and sends oversized single files alone',()=>{
    const batches=buildUploadBatches([{size:MAX_UPLOAD_BYTES},{size:1},{size:MAX_UPLOAD_BYTES+1},{size:1}]);
    expect(batches.map(b=>b.length)).toEqual([1,1,1,1]);
  });
  it('handles empty files and exact size boundary',()=>{
    expect(buildUploadBatches([])).toEqual([]);
    expect(buildUploadBatches([{size:1},{size:MAX_UPLOAD_BYTES-1}])).toHaveLength(1);
  });
  it('does not claim completion before server confirmation',()=>{expect(uploadPercent(100,100,200)).toBe(99);expect(uploadPercent(100,0,200)).toBe(50);expect(uploadPercent(0,0,0)).toBe(0);});
  it('maps name templates to legacy modes',()=>{expect(childTemplateMode('【動画】')).toBe('movie');expect(childTemplateMode('【加工回し】')).toBe('process');expect(childTemplateMode('【構図】')).toBe('normal');});
});
