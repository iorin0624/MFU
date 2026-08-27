(function (global) {
  'use strict';

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function createKey() {
    return global.crypto?.randomUUID?.()
      || `${Date.now()}${Math.random().toString(16).slice(2)}`;
  }

  async function readProgress(key) {
    const response = await fetch(`/api/zip-progress?key=${encodeURIComponent(key)}`, {
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) return null;
    return response.json();
  }

  function startProgressPolling(key, onProgress, intervalMs = 500) {
    let stopped = false;
    let timer = null;
    let socket = null;

    const once = async () => {
      if (stopped) return null;
      try {
        const progress = await readProgress(key);
        if (progress && typeof onProgress === 'function') onProgress(progress);
        return progress;
      } catch (_) {
        return null;
      }
    };

    const startFallback = () => {
      if (stopped || timer) return;
      timer = global.setInterval(once, Math.max(3000, intervalMs));
    };
    const stopFallback = () => {
      if (timer) global.clearInterval(timer);
      timer = null;
    };
    if (typeof global.io === 'function') {
      socket = global.io('/download-progress', {transports:['websocket','polling']});
      socket.on('connect', () => {
        stopFallback();
        socket.emit('zip_progress_subscribe', {key}, (reply) => {
          if (reply?.progress && typeof onProgress === 'function') onProgress(reply.progress);
        });
      });
      socket.on('zip_progress_update', (payload) => {
        if (payload?.key === key && payload.progress && typeof onProgress === 'function') {
          onProgress(payload.progress);
        }
      });
      socket.on('disconnect', startFallback);
      socket.on('connect_error', startFallback);
    } else {
      startFallback();
    }
    return {
      once,
      stop() {
        stopped = true;
        stopFallback();
        socket?.disconnect();
        socket = null;
      },
    };
  }

  async function prepare({ paths, csrfToken = '', key = createKey(), onProgress = null }) {
    const poller = startProgressPolling(key, onProgress);
    try {
      const response = await fetch('/api/zip-prepare', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken,
          'X-Idempotency-Key': key,
        },
        body: JSON.stringify({ paths }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.ok || !data.download_url) {
        const error = new Error(data?.error || `zip_prepare_failed_${response.status}`);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      const finalProgress = await poller.once();
      return { ...data, key, progress: finalProgress };
    } finally {
      poller.stop();
    }
  }

  async function waitUntilReady({ key, onProgress = null, intervalMs = 700 }) {
    return new Promise((resolve, reject) => {
      const watcher = startProgressPolling(key, (progress) => {
        if (typeof onProgress === 'function') onProgress(progress);
        if (progress.status === 'done') { watcher.stop(); resolve(progress); }
        if (progress.status === 'error') { watcher.stop(); reject(new Error(progress.message || 'zip_failed')); }
      }, intervalMs);
      watcher.once().then((progress) => {
        if (!progress) return;
        if (typeof onProgress === 'function') onProgress(progress);
        if (progress.status === 'done') { watcher.stop(); resolve(progress); }
        if (progress.status === 'error') { watcher.stop(); reject(new Error(progress.message || 'zip_failed')); }
      }).catch(() => {});
    });
  }

  function startDownload(downloadUrl) {
    global.location.assign(downloadUrl);
  }

  global.MFUZipDownload = Object.freeze({
    createKey,
    prepare,
    readProgress,
    startDownload,
    startProgressPolling,
    waitUntilReady,
  });
})(window);
