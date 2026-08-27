(() => {
  if (globalThis.__ntpScheduledReloadRunner) return;

  const state = {
    config: null,
    timer: null,
    cancelled: false,
    t5Requested: false,
    executed: false,
    basePerformance: 0,
    baseNtpEpoch: 0,
    slewStartPerformance: 0,
    slewEndPerformance: 0,
    slewDeltaMs: 0
  };

  function ntpNow() {
    const performanceNow = performance.now();
    const elapsed = performanceNow - state.basePerformance;
    let correction = 0;
    if (state.slewEndPerformance > state.slewStartPerformance) {
      const progress = Math.max(0, Math.min(1,
        (performanceNow - state.slewStartPerformance) /
        (state.slewEndPerformance - state.slewStartPerformance)
      ));
      correction = state.slewDeltaMs * progress;
    }
    return state.baseNtpEpoch + elapsed + correction;
  }

  function setInitialOffset(offsetMs) {
    state.basePerformance = performance.now();
    state.baseNtpEpoch = Date.now() + Number(offsetMs || 0);
    state.slewStartPerformance = 0;
    state.slewEndPerformance = 0;
    state.slewDeltaMs = 0;
  }

  function applyOffsetWithoutReversing(offsetMs) {
    const performanceNow = performance.now();
    const current = ntpNow();
    const desired = Date.now() + Number(offsetMs || 0);
    const delta = desired - current;
    state.basePerformance = performanceNow;
    state.baseNtpEpoch = current;
    state.slewDeltaMs = delta;
    state.slewStartPerformance = performanceNow;
    state.slewEndPerformance = delta < 0 ? performanceNow + 2000 : performanceNow + 250;
  }

  function sendError(error) {
    if (!state.config) return;
    chrome.runtime.sendMessage({
      type: "RUNNER_ERROR",
      reservationId: state.config.reservationId,
      error: error.message || String(error)
    }).catch(() => {});
  }

  async function requestT5Sync() {
    if (state.t5Requested || !state.config) return;
    state.t5Requested = true;
    try {
      const response = await chrome.runtime.sendMessage({
        type: "RUNNER_T5_SYNC",
        reservationId: state.config.reservationId
      });
      if (!response?.ok) throw new Error(response?.error || "T-5秒同期に失敗しました。");
      applyOffsetWithoutReversing(response.sync.offsetMs);
    } catch (error) {
      // T-30以前の正常サンプルがあれば維持し、存在しない場合だけ停止します。
      if (!state.config.syncMeasuredAt && state.config.ntpFailureMode !== "pc_clock") {
        state.cancelled = true;
        sendError(error);
      }
    }
  }

  function executeReload() {
    if (state.executed || state.cancelled || !state.config) return;
    state.executed = true;
    const executeAt = state.config.targetEpochMs + state.config.executeOffsetMs;
    const actualExecutionNtpTime = ntpNow();
    chrome.runtime.sendMessage({
      type: "RUNNER_EXECUTING",
      reservationId: state.config.reservationId,
      actualExecutionNtpTime,
      executionErrorMs: actualExecutionNtpTime - executeAt
    }).catch(() => {});
    window.location.reload();
  }

  function finalWait(executeAt) {
    if (state.config.waitMode === "spin20") {
      while (!state.cancelled && ntpNow() < executeAt) {
        // 最終20ms程度だけmonotonic clockで待機します。
      }
      if (!state.cancelled) executeReload();
      return;
    }
    // 比較用タイマーモード。再帰呼び出しにせずイベントループへ戻します。
    state.timer = setTimeout(scheduleNext, 0);
  }

  function scheduleNext() {
    clearTimeout(state.timer);
    if (state.cancelled || state.executed || !state.config) return;
    const executeAt = state.config.targetEpochMs + state.config.executeOffsetMs;
    const remaining = executeAt - ntpNow();
    if (remaining <= 0) {
      if (Math.abs(remaining) > state.config.lateToleranceMs) {
        state.cancelled = true;
        sendError(new Error("予定時刻を1秒以上超過したため実行しませんでした。"));
      } else {
        executeReload();
      }
      return;
    }
    if (remaining <= 5500 && !state.t5Requested) requestT5Sync();
    if (remaining <= 20) {
      finalWait(executeAt);
      return;
    }
    const delay = remaining > 60000 ? 1000
      : remaining > 10000 ? 250
        : remaining > 1000 ? 50
          : remaining > 100 ? 10
            : 2;
    state.timer = setTimeout(scheduleNext, Math.min(delay, Math.max(1, remaining - 20)));
  }

  function configure(config) {
    if (!config?.reservationId || !Number.isFinite(Number(config.targetEpochMs))) {
      throw new Error("高精度待機の設定が不完全です。");
    }
    clearTimeout(state.timer);
    state.config = {...config};
    state.cancelled = false;
    state.executed = false;
    state.t5Requested = config.lastStage === "t5";
    setInitialOffset(config.ntpOffsetMs);
    chrome.runtime.sendMessage({type: "RUNNER_ARMED", reservationId: config.reservationId}).catch(() => {});
    scheduleNext();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "NTP_RELOAD_CONFIGURE") {
      try {
        configure(message.config);
        sendResponse({ok: true});
      } catch (error) {
        sendResponse({ok: false, error: error.message});
      }
      return;
    }
    if (message?.type === "NTP_RELOAD_CANCEL" && (!state.config || message.reservationId === state.config.reservationId)) {
      state.cancelled = true;
      clearTimeout(state.timer);
      sendResponse({ok: true});
    }
  });

  globalThis.__ntpScheduledReloadRunner = {configure, cancel: () => { state.cancelled = true; clearTimeout(state.timer); }};
})();
