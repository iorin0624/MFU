import {
  STORAGE_RESERVATIONS,
  STORAGE_LOGS,
  DEFAULT_EXECUTE_OFFSET_MS,
  DEFAULT_LATE_TOLERANCE_MS,
  MAX_LOGS,
  STATUS_LABELS,
  alarmName,
  parseAlarmName
} from "./common/constants.js";
import {synchronizeNtp} from "./common/ntp_sync.js";
import {isSupportedTarget, normalizedPageKey, nowFromOffset} from "./common/time.js";

const RUNNER_FILE = "content/precision_runner.js";

async function readReservations() {
  const stored = await chrome.storage.local.get(STORAGE_RESERVATIONS);
  return Array.isArray(stored[STORAGE_RESERVATIONS]) ? stored[STORAGE_RESERVATIONS] : [];
}

async function writeReservations(reservations) {
  await chrome.storage.local.set({[STORAGE_RESERVATIONS]: reservations});
}

async function mutateReservation(id, updater) {
  const reservations = await readReservations();
  const index = reservations.findIndex(item => item.id === id);
  if (index < 0) return null;
  const updated = updater({...reservations[index]});
  reservations[index] = updated;
  await writeReservations(reservations);
  return updated;
}

async function appendLog(reservationId, event, details = {}) {
  const stored = await chrome.storage.local.get(STORAGE_LOGS);
  const logs = Array.isArray(stored[STORAGE_LOGS]) ? stored[STORAGE_LOGS] : [];
  logs.unshift({id: crypto.randomUUID(), reservationId, event, at: Date.now(), ...details});
  await chrome.storage.local.set({[STORAGE_LOGS]: logs.slice(0, MAX_LOGS)});
}

async function clearReservationAlarms(id) {
  await Promise.all([
    chrome.alarms.clear(alarmName("t120", id)),
    chrome.alarms.clear(alarmName("t30", id))
  ]);
}

async function createPersistentAlarm(name, when) {
  if (when <= Date.now()) return false;
  await chrome.alarms.create(name, {when, persistAcrossSessions: true});
  return true;
}

async function scheduleReservationAlarms(reservation) {
  await clearReservationAlarms(reservation.id);
  const executeAt = reservation.targetEpochMs + reservation.executeOffsetMs;
  const t120 = executeAt - 120000;
  const t30 = executeAt - 30000;
  const now = Date.now();
  if (t120 > now) await createPersistentAlarm(alarmName("t120", reservation.id), t120);
  if (t30 > now) await createPersistentAlarm(alarmName("t30", reservation.id), t30);

  if (t120 <= now) {
    await runStage(reservation.id, "t120");
  }
  if (t30 <= now) {
    await runStage(reservation.id, "t30");
  }
}

function urlsCompatible(currentUrl, targetUrl) {
  try {
    return normalizedPageKey(currentUrl) === normalizedPageKey(targetUrl);
  } catch (_) {
    return false;
  }
}

async function resolveTargetTab(reservation) {
  if (Number.isInteger(reservation.tabId)) {
    try {
      const tab = await chrome.tabs.get(reservation.tabId);
      if (tab && tab.url && urlsCompatible(tab.url, reservation.targetUrl)) return tab;
    } catch (_) {
      // URLで再関連付けします。
    }
  }
  const candidates = (await chrome.tabs.query({})).filter(tab => tab.url && urlsCompatible(tab.url, reservation.targetUrl));
  if (candidates.length !== 1) {
    throw new Error(candidates.length ? "対象ページの候補が複数あります。" : "対象タブが存在しません。");
  }
  const tab = candidates[0];
  await mutateReservation(reservation.id, item => ({...item, tabId: tab.id, windowId: tab.windowId}));
  return tab;
}

function runnerConfiguration(reservation) {
  return {
    reservationId: reservation.id,
    targetEpochMs: reservation.targetEpochMs,
    executeOffsetMs: reservation.executeOffsetMs,
    ntpOffsetMs: Number(reservation.lastSync?.offsetMs || 0),
    syncMeasuredAt: reservation.lastSync?.measuredAt || null,
    ntpFailureMode: reservation.ntpFailureMode || "abort",
    lateToleranceMs: reservation.lateToleranceMs ?? DEFAULT_LATE_TOLERANCE_MS,
    waitMode: reservation.waitMode || "spin20",
    lastStage: reservation.lastStage || null
  };
}

async function configureRunner(reservation, tab) {
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: [RUNNER_FILE]});
  await chrome.tabs.sendMessage(tab.id, {type: "NTP_RELOAD_CONFIGURE", config: runnerConfiguration(reservation)});
}

async function focusTarget(tab) {
  await chrome.tabs.update(tab.id, {active: true});
  if (Number.isInteger(tab.windowId)) await chrome.windows.update(tab.windowId, {focused: true});
}

async function syncForReservation(reservation, count) {
  try {
    return await synchronizeNtp({count, previousOffsetMs: reservation.lastSync?.offsetMs ?? null});
  } catch (error) {
    if (reservation.ntpFailureMode === "pc_clock" && !reservation.lastSync) {
      return {
        offsetMs: 0, rttMs: null, measuredAt: Date.now(), sampleCount: 0,
        referenceId: "PC時計（強行）", stratum: null, leapStatus: "Fallback",
        fallback: true, warning: error.message
      };
    }
    throw error;
  }
}

async function failReservation(id, message) {
  const reservation = await mutateReservation(id, item => ({
    ...item, status: "error", enabled: false, error: message, updatedAt: Date.now()
  }));
  await clearReservationAlarms(id);
  if (reservation?.tabId) {
    chrome.tabs.sendMessage(reservation.tabId, {type: "NTP_RELOAD_CANCEL", reservationId: id}).catch(() => {});
  }
  await appendLog(id, "error", {message});
  return reservation;
}

async function runStage(id, stage) {
  let reservation = (await readReservations()).find(item => item.id === id);
  if (!reservation || !reservation.enabled || ["completed", "cancelled", "error"].includes(reservation.status)) return;
  const executeAt = reservation.targetEpochMs + reservation.executeOffsetMs;
  if (Date.now() > executeAt + reservation.lateToleranceMs) {
    await failReservation(id, "予定時刻を1秒以上超過したため実行しませんでした。");
    return;
  }
  try {
    reservation = await mutateReservation(id, item => ({...item, status: "synchronizing", error: "", updatedAt: Date.now()}));
    const tab = await resolveTargetTab(reservation);
    const sample = await syncForReservation(reservation, 5);
    reservation = await mutateReservation(id, item => ({
      ...item,
      tabId: tab.id,
      windowId: tab.windowId,
      status: stage === "t30" ? "armed" : "preparing",
      lastSync: sample,
      lastStage: stage,
      updatedAt: Date.now()
    }));
    if (stage === "t30" && reservation.bringToFront !== false) await focusTarget(tab);
    await configureRunner(reservation, tab);
    await appendLog(id, stage === "t30" ? "armed" : "prepared", {
      offsetMs: sample.offsetMs,
      rttMs: sample.rttMs,
      sampleCount: sample.sampleCount
    });
  } catch (error) {
    await failReservation(id, error.message || String(error));
  }
}

async function handleT5Sync(message, sender) {
  const reservations = await readReservations();
  const reservation = reservations.find(item => item.id === message.reservationId);
  if (!reservation || !reservation.enabled) throw new Error("予約が無効です。");
  if (!sender.tab || sender.tab.id !== reservation.tabId) throw new Error("対象タブが一致しません。");
  const sample = await syncForReservation(reservation, 3);
  const updated = await mutateReservation(reservation.id, item => ({
    ...item, lastSync: sample, status: "armed", lastStage: "t5", updatedAt: Date.now()
  }));
  await appendLog(reservation.id, "t5_sync", {offsetMs: sample.offsetMs, rttMs: sample.rttMs, sampleCount: sample.sampleCount});
  return {ok: true, sync: updated.lastSync};
}

async function createReservation(payload) {
  if (!isSupportedTarget(payload.targetUrl)) throw new Error("HTTP/HTTPSページだけを対象にできます。");
  const reservation = {
    id: crypto.randomUUID(),
    enabled: true,
    status: "scheduled",
    targetEpochMs: Number(payload.targetEpochMs),
    executeOffsetMs: Math.max(-1000, Math.min(5000, Number(payload.executeOffsetMs ?? DEFAULT_EXECUTE_OFFSET_MS))),
    targetUrl: payload.targetUrl,
    targetTitle: payload.targetTitle || payload.targetUrl,
    tabId: payload.tabId,
    windowId: payload.windowId,
    bringToFront: payload.bringToFront !== false,
    ntpFailureMode: payload.ntpFailureMode === "pc_clock" ? "pc_clock" : "abort",
    lateToleranceMs: DEFAULT_LATE_TOLERANCE_MS,
    waitMode: payload.waitMode === "timer" ? "timer" : "spin20",
    createdAt: Date.now(),
    updatedAt: Date.now(),
    lastSync: null,
    error: ""
  };
  if (!Number.isFinite(reservation.targetEpochMs)) throw new Error("実行日時が不正です。");
  if (reservation.targetEpochMs + reservation.executeOffsetMs <= Date.now()) throw new Error("未来の日時を指定してください。");
  const reservations = await readReservations();
  reservations.unshift(reservation);
  await writeReservations(reservations);
  await appendLog(reservation.id, "scheduled", {targetEpochMs: reservation.targetEpochMs, executeOffsetMs: reservation.executeOffsetMs});
  await scheduleReservationAlarms(reservation);
  return reservation;
}

async function cancelReservation(id) {
  const reservation = await mutateReservation(id, item => ({...item, enabled: false, status: "cancelled", updatedAt: Date.now()}));
  if (!reservation) throw new Error("予約が見つかりません。");
  await clearReservationAlarms(id);
  if (reservation.tabId) chrome.tabs.sendMessage(reservation.tabId, {type: "NTP_RELOAD_CANCEL", reservationId: id}).catch(() => {});
  await appendLog(id, "cancelled");
  return reservation;
}

async function getState() {
  const [reservations, stored, tabs] = await Promise.all([
    readReservations(),
    chrome.storage.local.get(STORAGE_LOGS),
    chrome.tabs.query({active: true, currentWindow: true})
  ]);
  return {
    ok: true,
    reservations,
    logs: Array.isArray(stored[STORAGE_LOGS]) ? stored[STORAGE_LOGS] : [],
    currentTab: tabs[0] || null,
    statusLabels: STATUS_LABELS
  };
}

async function handleMessage(message, sender) {
  switch (message?.type) {
    case "GET_STATE": return getState();
    case "GET_NTP_STATUS": return {ok: true, sync: await synchronizeNtp({count: message.count || 5})};
    case "CREATE_RESERVATION": return {ok: true, reservation: await createReservation(message.reservation)};
    case "CREATE_TEST_RESERVATION": {
      const sync = await synchronizeNtp({count: 5});
      return {ok: true, reservation: await createReservation({
        ...message.reservation,
        targetEpochMs: nowFromOffset(sync.offsetMs) + (Number(message.delaySeconds) * 1000)
      })};
    }
    case "CANCEL_RESERVATION": return {ok: true, reservation: await cancelReservation(message.reservationId)};
    case "RUNNER_T5_SYNC": return handleT5Sync(message, sender);
    case "RUNNER_ARMED":
      await mutateReservation(message.reservationId, item => ({...item, status: "armed", updatedAt: Date.now()}));
      return {ok: true};
    case "RUNNER_EXECUTING":
      await mutateReservation(message.reservationId, item => ({...item, status: "executing", actualExecutionNtpTime: message.actualExecutionNtpTime, executionErrorMs: message.executionErrorMs, updatedAt: Date.now()}));
      await appendLog(message.reservationId, "executing", {actualExecutionNtpTime: message.actualExecutionNtpTime, executionErrorMs: message.executionErrorMs});
      return {ok: true};
    case "RUNNER_ERROR":
      await failReservation(message.reservationId, message.error || "高精度待機でエラーが発生しました。");
      return {ok: true};
    default: throw new Error("未対応のメッセージです。");
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then(result => sendResponse(result))
    .catch(error => sendResponse({ok: false, error: error.message || String(error)}));
  return true;
});

chrome.alarms.onAlarm.addListener(alarm => {
  const parsed = parseAlarmName(alarm.name);
  if (parsed && (parsed.stage === "t120" || parsed.stage === "t30")) runStage(parsed.reservationId, parsed.stage);
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  const reservations = await readReservations();
  const reservation = reservations.find(item => item.enabled && item.tabId === tabId);
  if (!reservation) return;
  const executeAt = reservation.targetEpochMs + reservation.executeOffsetMs;

  if (changeInfo.url && !urlsCompatible(changeInfo.url, reservation.targetUrl)) {
    await failReservation(reservation.id, "対象ページが変更されたため実行を中止しました。");
    return;
  }
  if (changeInfo.status === "loading" && reservation.status === "executing") return;
  if (changeInfo.status !== "complete" || !["preparing", "synchronizing", "armed"].includes(reservation.status)) return;
  const remaining = executeAt - Date.now();
  if (remaining <= reservation.lateToleranceMs) {
    await failReservation(reservation.id, "実行直前に対象タブが再読み込みされたため安全中止しました。");
    return;
  }
  try {
    await configureRunner(reservation, tab);
    await appendLog(reservation.id, "runner_reinjected", {remainingMs: remaining});
  } catch (error) {
    await failReservation(reservation.id, `runnerを再挿入できませんでした: ${error.message}`);
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.status !== "complete") return;
  const reservations = await readReservations();
  const reservation = reservations.find(item => item.enabled && item.tabId === tabId && item.status === "executing");
  if (!reservation) return;
  await mutateReservation(reservation.id, item => ({...item, enabled: false, status: "completed", completedAt: Date.now(), updatedAt: Date.now()}));
  await clearReservationAlarms(reservation.id);
  await appendLog(reservation.id, "completed", {completedAt: Date.now()});
});

async function restoreReservations() {
  const reservations = await readReservations();
  for (const reservation of reservations.filter(item => item.enabled && ["scheduled", "preparing", "synchronizing", "armed"].includes(item.status))) {
    try {
      await scheduleReservationAlarms(reservation);
    } catch (error) {
      await failReservation(reservation.id, `予約を復元できませんでした: ${error.message}`);
    }
  }
}

chrome.runtime.onInstalled.addListener(() => restoreReservations());
chrome.runtime.onStartup.addListener(() => restoreReservations());
restoreReservations();
