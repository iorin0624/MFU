export const PI_TIME_API = "http://192.168.103.15:5055/chrony/time";
export const STORAGE_RESERVATIONS = "ntpReloadReservations";
export const STORAGE_LOGS = "ntpReloadLogs";
export const DEFAULT_EXECUTE_OFFSET_MS = 0;
export const DEFAULT_LATE_TOLERANCE_MS = 1000;
export const MAX_LOGS = 300;
export const OFFSET_JUMP_LIMIT_MS = 100;
export const RETRY_CONSISTENCY_MS = 25;
export const MAX_ACCEPTED_RTT_MS = 1000;

export const STATUS_LABELS = {
  scheduled: "待機中",
  preparing: "準備中",
  synchronizing: "NTP同期中",
  armed: "実行準備完了",
  executing: "リロード開始",
  completed: "リロード済み",
  cancelled: "キャンセル",
  error: "エラー"
};

export const ALARM_PREFIX = "ntp-scheduled-reload";

export function alarmName(stage, reservationId) {
  return `${ALARM_PREFIX}:${stage}:${reservationId}`;
}

export function parseAlarmName(name) {
  const [prefix, stage, reservationId] = String(name || "").split(":");
  return prefix === ALARM_PREFIX && stage && reservationId
    ? {stage, reservationId}
    : null;
}
