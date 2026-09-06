<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { portalApi } from '@/api/client';
import { usePortalStore } from '@/stores/portal';

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

const pushSupported = ref(true);
const pushEnabled = ref(false);
const pushBusy = ref(false);
const pushMessage = ref('');
const updateOpen = ref(false);
const updateText = ref('');
const updateSeen = ref(false);
const standalone = ref(false);
const installPrompt = ref<InstallPromptEvent | null>(null);
let registration: ServiceWorkerRegistration | null = null;
let pushCsrf = '';
let vapidKey = '';
const store = usePortalStore();

function toUint8(value: string) {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from([...window.atob(base64)].map((char) => char.charCodeAt(0)));
}

async function initPush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    pushSupported.value = false;
    return;
  }
  try {
    const response = await fetch('/chat/api/push/bootstrap', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('初期化に失敗しました。');
    const data = await response.json();
    pushCsrf = data.csrf_token || '';
    vapidKey = data.vapid_public_key || '';
    registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    try { await registration.update(); }
    catch (reason) { console.debug('[sw] update check failed', reason); }
    pushEnabled.value = Boolean(await registration.pushManager.getSubscription());
    await store.refreshUnread();
  } catch (reason) {
    pushSupported.value = false;
    pushMessage.value = reason instanceof Error ? reason.message : 'Push通知を初期化できませんでした。';
  }
}

async function togglePush() {
  if (!registration) return;
  pushBusy.value = true;
  pushMessage.value = '';
  try {
    const current = await registration.pushManager.getSubscription();
    if (current) {
      const endpoint = current.endpoint;
      await current.unsubscribe();
      await fetch('/chat/api/push/unsubscribe', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ csrf_token: pushCsrf, endpoint }) });
      pushEnabled.value = false;
      pushMessage.value = 'Push通知を無効にしました。';
      return;
    }
    if (!vapidKey) throw new Error('Push通知の公開鍵が未設定です。');
    if (await Notification.requestPermission() !== 'granted') throw new Error('通知の許可が必要です。');
    const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toUint8(vapidKey) });
    const response = await fetch('/chat/api/push/subscribe', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ csrf_token: pushCsrf, sw_scope: new URL(registration.scope).pathname || '/', ...subscription.toJSON() }) });
    if (!response.ok) throw new Error('Push通知の登録に失敗しました。');
    pushEnabled.value = true;
    pushMessage.value = 'Push通知を有効にしました。';
  } catch (reason) {
    pushMessage.value = reason instanceof Error ? reason.message : 'Push通知の設定に失敗しました。';
  } finally { pushBusy.value = false; }
}

async function showUpdates(auto = false) {
  try {
    const data = await portalApi.updatesCheck();
    if (auto && !data.show) return;
    updateText.value = data.text || '（現在、アップデート情報はありません）';
    updateSeen.value = Boolean(data.seen);
    updateOpen.value = true;
  } catch { if (!auto) updateOpen.value = true; }
}

async function closeUpdates() {
  if (updateSeen.value) {
    try { await portalApi.updatesAck(); } catch { /* non-critical */ }
  }
  updateOpen.value = false;
}

function captureInstallPrompt(event: Event) {
  event.preventDefault();
  installPrompt.value = event as InstallPromptEvent;
}

async function installPwa() {
  if (!installPrompt.value) return;
  await installPrompt.value.prompt();
  await installPrompt.value.userChoice;
  installPrompt.value = null;
}

onMounted(() => {
  standalone.value = window.matchMedia('(display-mode: standalone)').matches || Boolean((navigator as Navigator & { standalone?: boolean }).standalone);
  window.addEventListener('beforeinstallprompt', captureInstallPrompt);
  void initPush();
  void showUpdates(true);
});
onBeforeUnmount(() => window.removeEventListener('beforeinstallprompt', captureInstallPrompt));
</script>

<template>
  <section class="portal-utilities" aria-label="アプリ設定">
    <button type="button" class="utility-button" :disabled="!pushSupported || pushBusy" @click="togglePush">🔔 {{ pushEnabled ? 'Push通知を無効化' : 'Push通知を有効化' }}</button>
    <button type="button" class="utility-button" @click="showUpdates(false)">🆕 アップデート情報</button>
    <button v-if="!standalone && installPrompt" type="button" class="utility-button" @click="installPwa">📲 アプリを追加</button>
    <span v-if="pushMessage" class="utility-message">{{ pushMessage }}</span>
  </section>
  <aside v-if="!standalone && !installPrompt" class="pwa-guide">
    <strong>📲 ホーム画面に追加できます</strong>
    <span>iPhone / iPadはSafariの共有ボタンから「ホーム画面に追加」を選択してください。</span>
  </aside>

  <div v-if="updateOpen" class="modal-backdrop" @click.self="closeUpdates"><div class="modal-card"><h2>アップデート情報</h2><div class="update-text">{{ updateText }}</div><label class="update-seen"><input v-model="updateSeen" type="checkbox">次回以降、自動表示しない</label><div class="modal-actions"><button type="button" class="button primary" @click="closeUpdates">OK</button></div></div></div>
</template>
