import test from "node:test";
import assert from "node:assert/strict";
import {parseJstEpoch, formatCountdown, normalizedPageKey} from "../common/time.js";
import {summarizeErrors} from "../common/stats.js";
import {validatePiPayload} from "../common/ntp_sync.js";

test("JST日時をepoch msへ変換する",()=>assert.equal(parseJstEpoch("2026-09-01","10:00:00.123"),1788224400123));
test("時・分だけのJST日時は00秒として変換する",()=>assert.equal(parseJstEpoch("2026-09-01","10:00"),1788224400000));
test("ミリ秒付きカウントダウン",()=>assert.equal(formatCountdown(321482),"00:05:21.482"));
test("query/hashを除外して同一ページを判定",()=>assert.equal(normalizedPageKey("https://example.com/a?b=1#c"),"https://example.com/a"));
test("誤差統計",()=>assert.deepEqual(summarizeErrors([1,2,3,4,5]),{count:5,mean:3,median:3,earliest:1,latest:5,standardDeviation:Math.sqrt(2),p95:5,p99:5}));
test("Pi payloadの正常性",()=>assert.equal(validatePiPayload({ok:true,leap_status:"Normal",stratum:2,ntp_time_unix_ms:1,command_duration_ms:10}).stratum,2));
test("Leap異常を拒否",()=>assert.throws(()=>validatePiPayload({ok:true,leap_status:"Not synchronised",stratum:2,ntp_time_unix_ms:1}),/Leap status/));
