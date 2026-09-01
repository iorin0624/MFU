import { describe, expect, it } from 'vitest';
import { cropRect } from './avatarCrop';
describe('avatar crop geometry',()=>{
  it('centers portrait and landscape with a square crop',()=>{expect(cropRect(1000,2000,1,0,0)).toEqual({x:0,y:500,size:1000});expect(cropRect(2000,1000,1,0,0)).toEqual({x:500,y:0,size:1000});});
  it('supports zoom and edge positioning',()=>{expect(cropRect(1000,2000,2,1,-1)).toEqual({x:500,y:0,size:500});});
  it('never samples beyond image bounds',()=>{for(const zoom of [0,1,2,4,10])for(const pos of [-4,-1,0,1,4]){const rect=cropRect(1000,2000,zoom,pos,pos);expect(rect.x).toBeGreaterThanOrEqual(0);expect(rect.y).toBeGreaterThanOrEqual(0);expect(rect.x+rect.size).toBeLessThanOrEqual(1000);expect(rect.y+rect.size).toBeLessThanOrEqual(2000);}});
});
