import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatViewportStyle, trackChatViewport } from './chatViewport';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
describe('mobile chat viewport coordinates', () => {
  it('uses document coordinates when Safari pans the visual viewport', () => {
    expect(chatViewportStyle({ height:280, pageTop:152, offsetTop:52 }, 667, 100)).toEqual({
      '--chat-viewport-height':'280px', '--chat-viewport-page-top':'152px',
    });
  });
  it('does not freeze the keyboard height when the viewport is zoomed', () => {
    const zoomed = { height:240, pageTop:80, offsetTop:80, scale:1.2 };
    expect(chatViewportStyle(zoomed, 667, 0)['--chat-viewport-height']).toBe('240px');
  });
  it('restores height and position after the keyboard closes', () => {
    expect(chatViewportStyle({ height:600, pageTop:0, offsetTop:0 }, 667, 0)).toEqual({
      '--chat-viewport-height':'600px', '--chat-viewport-page-top':'0px',
    });
  });
  it('falls back without the VisualViewport API', () => {
    expect(chatViewportStyle(null, 667, 30)).toEqual({
      '--chat-viewport-height':'667px', '--chat-viewport-page-top':'30px',
    });
  });
  it('remeasures after focus animation and removes all tracking on unmount', () => {
    vi.useFakeTimers();
    const viewport = Object.assign(new EventTarget(), { height:600, pageTop:0, offsetTop:0 });
    vi.stubGlobal('visualViewport', viewport);
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => window.setTimeout(() => fn(0),16));
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
    const apply = vi.fn();
    const stop = trackChatViewport(apply);
    document.dispatchEvent(new Event('focusin'));
    vi.advanceTimersByTime(100);
    viewport.height=260; viewport.pageTop=70;
    vi.advanceTimersByTime(350);
    expect(apply).toHaveBeenLastCalledWith({ '--chat-viewport-height':'260px', '--chat-viewport-page-top':'70px' });
    stop();
    const count = apply.mock.calls.length;
    document.dispatchEvent(new Event('focusout'));
    viewport.dispatchEvent(new Event('resize'));
    vi.runAllTimers();
    expect(apply).toHaveBeenCalledTimes(count);
  });
});
