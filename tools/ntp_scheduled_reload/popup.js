import {parseJstEpoch, formatJstMinute, formatCountdown, originPermissionPattern} from "./common/time.js";

let state={reservations:[],logs:[],currentTab:null,statusLabels:{}}; let ntpOffset=0;let newestReservationId=null;
const $=id=>document.getElementById(id);
function show(message,error=false){const el=$("message");el.textContent=message;el.className=error?"error":"";el.style.display="block";setTimeout(()=>el.style.display="none",5000)}
function defaultTarget(){const d=new Date(Date.now()+5*60000);const j=new Date(d.getTime()+9*3600000);j.setUTCSeconds(0,0);return j.toISOString().slice(0,16)}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function send(message){const response=await chrome.runtime.sendMessage(message);if(!response?.ok)throw new Error(response?.error||"処理に失敗しました。");return response}
async function refresh(){
  const response=await send({type:"GET_STATE"});state=response;
  $("tab-title").textContent=state.currentTab?.title||"対象外のタブ";$("tab-url").textContent=state.currentTab?.url||"";
  render();
}
function render(){
  const reservations=(Array.isArray(state.reservations)?state.reservations:[]).filter(r=>r.enabled&&!['cancelled','completed','error'].includes(r.status));
  const logs=Array.isArray(state.logs)?state.logs:[];
  $("reservation-count").textContent=`${reservations.length}件`;
  $("reservations").innerHTML=reservations.map(r=>`<article id="reservation-${r.id}" class="reservation ${r.id===newestReservationId?"reservation-new":""}"><header><b>${esc(r.targetTitle)}</b><span>${esc(state.statusLabels?.[r.status]||r.status)}</span></header><div>${formatJstMinute(r.targetEpochMs+r.executeOffsetMs)}</div><div class="countdown" data-countdown="${r.targetEpochMs+r.executeOffsetMs}"></div><small>補正 ${Number(r.executeOffsetMs)>=0?"+":""}${r.executeOffsetMs}ms / NTP ${r.lastSync?Number(r.lastSync.offsetMs).toFixed(3)+"ms":"未同期"}</small>${r.error?`<small class="error">${esc(r.error)}</small>`:""}${r.enabled?`<button data-cancel="${r.id}">キャンセル</button>`:""}</article>`).join("")||"<small>予約はありません。</small>";
  $("logs").innerHTML=logs.slice(0,8).map(l=>`<div class="log"><b>${esc(l.event)}</b> ${new Date(l.at).toLocaleString("ja-JP")} ${l.executionErrorMs!=null?`<br>実行誤差 ${Number(l.executionErrorMs).toFixed(3)}ms`:""}</div>`).join("")||"<small>ログはありません。</small>";
}
async function ensureTargetPermission(url){const origin=originPermissionPattern(url);if(await chrome.permissions.contains({origins:[origin]}))return;const granted=await chrome.permissions.request({origins:[origin]});if(!granted)throw new Error("対象サイトへの権限が許可されませんでした。");}
function reservationInput(){if(!state.currentTab?.url)throw new Error("通常のWebページを開いてください。");const [date,time]=String($("target").value).split("T");return {targetEpochMs:parseJstEpoch(date,time),executeOffsetMs:Number($("offset").value),targetUrl:state.currentTab.url,targetTitle:state.currentTab.title,tabId:state.currentTab.id,windowId:state.currentTab.windowId,bringToFront:$("focus").checked,ntpFailureMode:$("failure").value,waitMode:"spin20"};}
$("form").addEventListener("submit",async e=>{e.preventDefault();try{const input=reservationInput();await ensureTargetPermission(input.targetUrl);const response=await send({type:"CREATE_RESERVATION",reservation:input});newestReservationId=response.reservation.id;state.reservations=[response.reservation,...(Array.isArray(state.reservations)?state.reservations.filter(r=>r.id!==response.reservation.id):[])];render();show("予約しました。");await refresh();$("reservation-section").scrollIntoView({behavior:"smooth",block:"start"})}catch(err){show(err.message,true)}});
document.addEventListener("click",async e=>{const cancel=e.target.closest("[data-cancel]");if(cancel){try{const id=cancel.dataset.cancel;state.reservations=(Array.isArray(state.reservations)?state.reservations:[]).filter(r=>r.id!==id);render();await send({type:"CANCEL_RESERVATION",reservationId:id});show("予約をキャンセルしました。");await refresh()}catch(err){show(err.message,true);await refresh().catch(()=>{})}}const test=e.target.closest("[data-test]");if(test){try{const input=reservationInput();await ensureTargetPermission(input.targetUrl);await send({type:"CREATE_TEST_RESERVATION",reservation:input,delaySeconds:Number(test.dataset.test)});show(`${test.dataset.test}秒後のテストを予約しました。`);await refresh()}catch(err){show(err.message,true)}}});
$("open-benchmark").addEventListener("click",()=>chrome.tabs.create({url:chrome.runtime.getURL("test/precision_test.html")}));
$("offset").addEventListener("input",()=>$("offset-warning").classList.toggle("hidden",Number($("offset").value)>=0));
setInterval(()=>document.querySelectorAll("[data-countdown]").forEach(el=>el.textContent=formatCountdown(Number(el.dataset.countdown)-(Date.now()+ntpOffset))),100);
$("target").value=defaultTarget();
send({type:"GET_NTP_STATUS",count:5}).then(r=>{ntpOffset=r.sync.offsetMs;$("ntp").innerHTML=`<b>● Raspberry Pi 接続正常</b><br>Offset ${Number(r.sync.offsetMs).toFixed(3)} ms / RTT ${Number(r.sync.rttMs).toFixed(3)} ms`;}).catch(e=>$("ntp").innerHTML=`<span class="error">NTP基準を取得できません: ${esc(e.message)}</span>`);
refresh().catch(e=>show(e.message,true));setInterval(()=>refresh().catch(()=>{}),1000);
chrome.storage.onChanged.addListener((changes,area)=>{if(area==="local"&&(changes.ntpReloadReservations||changes.ntpReloadLogs))refresh().catch(()=>{})});
