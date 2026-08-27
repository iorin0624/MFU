export function nowFromOffset(offsetMs) {
  return Date.now() + Number(offsetMs || 0);
}

export function parseJstEpoch(dateValue, timeValue) {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateValue || ""));
  const timeMatch = /^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(String(timeValue || ""));
  if (!dateMatch || !timeMatch) throw new Error("実行日時を正しく入力してください。");
  const [, year, month, day] = dateMatch;
  const [, hour, minute, second = "0", fraction = "0"] = timeMatch;
  const millisecond = Number(fraction.padEnd(3, "0"));
  const epoch = Date.UTC(
    Number(year), Number(month) - 1, Number(day),
    Number(hour) - 9, Number(minute), Number(second), millisecond
  );
  if (!Number.isFinite(epoch)) throw new Error("実行日時を変換できませんでした。");
  return epoch;
}

export function formatJstMinute(epochMs) {
  if (!Number.isFinite(Number(epochMs))) return "-";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false
  }).format(new Date(Number(epochMs)));
}

export function formatJst(epochMs, includeMilliseconds = true) {
  if (!Number.isFinite(Number(epochMs))) return "-";
  const date = new Date(Number(epochMs));
  const parts = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false
  }).format(date);
  return includeMilliseconds
    ? `${parts}.${String(date.getUTCMilliseconds()).padStart(3, "0")}`
    : parts;
}

export function formatCountdown(milliseconds) {
  const sign = milliseconds < 0 ? "-" : "";
  const value = Math.abs(milliseconds);
  const hours = Math.floor(value / 3600000);
  const minutes = Math.floor((value % 3600000) / 60000);
  const seconds = Math.floor((value % 60000) / 1000);
  const millis = Math.floor(value % 1000);
  return `${sign}${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

export function normalizedPageKey(rawUrl) {
  const url = new URL(rawUrl);
  return `${url.origin}${url.pathname}`;
}

export function isSupportedTarget(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch (_) {
    return false;
  }
}

export function originPermissionPattern(rawUrl) {
  const url = new URL(rawUrl);
  return `${url.origin}/*`;
}
