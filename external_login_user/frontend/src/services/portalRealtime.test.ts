import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { onPortalEvent, onPortalResume, portalSocket, stopPortalRealtime } from './portalRealtime';

let callbacks: Map<string, Array<(payload?: any) => void>>;
let socket: any;
beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, 'hidden', {configurable:true,value:false});
  callbacks = new Map();
  socket = { connected:true, connect:vi.fn(), disconnect:vi.fn(), emit:vi.fn(), on:vi.fn((event,handler) => {
    callbacks.set(event,[...(callbacks.get(event)||[]),handler]);
  }) };
  window.io = vi.fn(() => socket);
});
afterEach(() => { stopPortalRealtime(); vi.clearAllTimers(); vi.useRealTimers(); delete window.io; });
describe('shared realtime lifecycle', () => {
  it('does not multiply socket handlers after revisiting a page', () => {
    portalSocket();
    const first=vi.fn(); const second=vi.fn();
    onPortalEvent('test_event',first)();
    const remove=onPortalEvent('test_event',second);
    callbacks.get('test_event')?.forEach((handler)=>handler({count:1}));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    remove();
  });
  it('reconciles on resume even if the socket still claims to be connected', () => {
    portalSocket();
    const resume=vi.fn(); const remove=onPortalResume(resume);
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('pageshow'));
    vi.advanceTimersByTime(200);
    expect(resume).toHaveBeenCalledTimes(1);
    remove();
  });
  it('reconnects a disconnected socket and does not refresh in the background', () => {
    portalSocket(); socket.connected=false;
    const resume=vi.fn(); const remove=onPortalResume(resume);
    Object.defineProperty(document,'hidden',{configurable:true,value:true});
    document.dispatchEvent(new Event('visibilitychange'));
    vi.advanceTimersByTime(200);
    expect(resume).not.toHaveBeenCalled();
    Object.defineProperty(document,'hidden',{configurable:true,value:false});
    window.dispatchEvent(new Event('online'));
    vi.advanceTimersByTime(200);
    expect(socket.connect).toHaveBeenCalledTimes(1);
    expect(resume).toHaveBeenCalledTimes(1);
    remove();
  });
});
