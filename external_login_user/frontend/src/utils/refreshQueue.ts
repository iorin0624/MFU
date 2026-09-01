/** Coalesce events without losing an event received during an HTTP request. */
export function createRefreshQueue(task: () => Promise<void>, delay = 80) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let running = false;
  let pending = false;
  let stopped = false;
  async function run() {
    timer = undefined;
    if (stopped || running) return;
    running = true;
    pending = false;
    try { await task(); }
    finally { running = false; if (pending && !stopped) schedule(); }
  }
  function schedule() {
    if (stopped) return;
    pending = true;
    if (!running && !timer) timer = setTimeout(() => { void run(); }, delay);
  }
  return { schedule, stop() { stopped = true; clearTimeout(timer); } };
}
