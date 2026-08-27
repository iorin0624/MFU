import {
  PI_TIME_API,
  MAX_ACCEPTED_RTT_MS,
  OFFSET_JUMP_LIMIT_MS,
  RETRY_CONSISTENCY_MS
} from "./constants.js";

export function validatePiPayload(payload) {
  if (!payload || payload.ok !== true) throw new Error("NTP基準を取得できません。");
  if (payload.leap_status !== "Normal") throw new Error(`Leap statusが正常ではありません: ${payload.leap_status || "不明"}`);
  if (!Number.isFinite(Number(payload.stratum))) throw new Error("Stratumを取得できません。");
  if (!Number.isFinite(Number(payload.ntp_time_unix_ms))) throw new Error("NTP時刻データが不完全です。");
  if (Number(payload.command_duration_ms || 0) > 3000) throw new Error("Piの時刻測定に時間がかかりすぎています。");
  return payload;
}

export async function takeNtpSample(fetchImpl = fetch) {
  const wallStart = Date.now();
  const perfStart = performance.now();
  const response = await fetchImpl(`${PI_TIME_API}?_=${Date.now()}`, {cache: "no-store"});
  const perfEnd = performance.now();
  const rttMs = perfEnd - perfStart;
  if (!response.ok) throw new Error(`Pi時刻API HTTP ${response.status}`);
  const payload = validatePiPayload(await response.json());
  if (rttMs > MAX_ACCEPTED_RTT_MS) throw new Error(`RTTが大きすぎます: ${rttMs.toFixed(1)}ms`);
  const clientMidpointMs = wallStart + (rttMs / 2);
  return {
    offsetMs: Number(payload.ntp_time_unix_ms) - clientMidpointMs,
    rttMs,
    measuredAt: Date.now(),
    serverSampledAt: payload.sampled_at,
    referenceId: payload.reference_id,
    stratum: Number(payload.stratum),
    leapStatus: payload.leap_status,
    commandDurationMs: Number(payload.command_duration_ms || 0)
  };
}

async function collectSamples(count, fetchImpl) {
  const samples = [];
  const errors = [];
  for (let index = 0; index < count; index += 1) {
    try {
      samples.push(await takeNtpSample(fetchImpl));
    } catch (error) {
      errors.push(error.message);
    }
  }
  if (samples.length < Math.min(3, count)) {
    throw new Error(errors[0] || "NTP測定の有効サンプルが不足しています。");
  }
  samples.sort((left, right) => left.rttMs - right.rttMs);
  return {best: samples[0], samples, errors};
}

export async function synchronizeNtp({count = 5, previousOffsetMs = null, fetchImpl = fetch} = {}) {
  const first = await collectSamples(count, fetchImpl);
  if (previousOffsetMs == null || Math.abs(first.best.offsetMs - previousOffsetMs) <= OFFSET_JUMP_LIMIT_MS) {
    return {...first.best, sampleCount: first.samples.length};
  }

  const retry = await collectSamples(count, fetchImpl);
  if (Math.abs(retry.best.offsetMs - first.best.offsetMs) > RETRY_CONSISTENCY_MS) {
    throw new Error("NTP offsetが安定しないため実行を停止しました。");
  }
  return {...retry.best, sampleCount: retry.samples.length, retried: true};
}
