import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRefreshQueue } from './refreshQueue';
afterEach(()=>vi.useRealTimers());
describe('thread refresh queue',()=>{
  it('coalesces bursts',async()=>{vi.useFakeTimers();const task=vi.fn().mockResolvedValue(undefined);const q=createRefreshQueue(task);q.schedule();q.schedule();await vi.advanceTimersByTimeAsync(100);expect(task).toHaveBeenCalledTimes(1);q.stop();});
  it('rechecks after an event arrives during a request',async()=>{vi.useFakeTimers();let done!:()=>void;const task=vi.fn().mockImplementationOnce(()=>new Promise<void>(r=>done=r)).mockResolvedValue(undefined);const q=createRefreshQueue(task);q.schedule();await vi.advanceTimersByTimeAsync(100);q.schedule();q.schedule();done();await vi.advanceTimersByTimeAsync(100);expect(task).toHaveBeenCalledTimes(2);q.stop();});
  it('does not refresh after cleanup',async()=>{vi.useFakeTimers();const task=vi.fn();const q=createRefreshQueue(task);q.schedule();q.stop();await vi.advanceTimersByTimeAsync(100);expect(task).not.toHaveBeenCalled();});
});
