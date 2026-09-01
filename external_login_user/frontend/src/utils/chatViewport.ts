type ViewportMetrics = Pick<VisualViewport, 'height' | 'pageTop' | 'offsetTop'>;

export function chatViewportStyle(viewport: ViewportMetrics | null, height: number, scrollY: number) {
  // The shell is document-positioned, so use pageTop, not fixed + offsetTop.
  // A focus/zoom transition must not leave the previous (taller) height frozen.
  return {
    '--chat-viewport-height': `${Math.max(0, viewport?.height ?? height)}px`,
    '--chat-viewport-page-top': `${Math.max(0, viewport?.pageTop ?? (scrollY + (viewport?.offsetTop ?? 0)))}px`,
  };
}

export function trackChatViewport(apply: (style: Record<string, string>) => void) {
  let frame = 0;
  let timers: number[] = [];
  const viewport = window.visualViewport;
  const measure = () => {
    frame = 0;
    apply(chatViewportStyle(viewport, window.innerHeight, window.scrollY));
  };
  const schedule = () => { if (!frame) frame = window.requestAnimationFrame(measure); };
  // Safari can finish its keyboard/panning animation after the focus event.
  const settle = () => {
    timers.forEach(window.clearTimeout);
    schedule();
    timers = [100, 300, 600, 1000].map((delay) => window.setTimeout(schedule, delay));
  };
  measure();
  viewport?.addEventListener('resize', schedule);
  viewport?.addEventListener('scroll', schedule);
  window.addEventListener('resize', settle);
  window.addEventListener('scroll', schedule, { passive: true });
  document.addEventListener('focusin', settle);
  document.addEventListener('focusout', settle);
  return () => {
    window.cancelAnimationFrame(frame);
    timers.forEach(window.clearTimeout);
    viewport?.removeEventListener('resize', schedule);
    viewport?.removeEventListener('scroll', schedule);
    window.removeEventListener('resize', settle);
    window.removeEventListener('scroll', schedule);
    document.removeEventListener('focusin', settle);
    document.removeEventListener('focusout', settle);
  };
}
