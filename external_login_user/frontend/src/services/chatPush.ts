import { portalApi, setChatCsrfToken } from '@/api/client';

function applicationServerKey(value: string): Uint8Array<ArrayBuffer> {
  const padded = `${value}${'='.repeat((4 - value.length % 4) % 4)}`.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index=0; index<raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

export async function chatPushState(): Promise<boolean> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return false;
  const registration = await navigator.serviceWorker.getRegistration('/');
  return Boolean(await registration?.pushManager.getSubscription());
}

export async function enableChatPush(): Promise<void> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('この端末はプッシュ通知に対応していません。');
  if (await Notification.requestPermission() !== 'granted') throw new Error('通知が許可されていません。');
  const bootstrap = await portalApi.chatPushBootstrap(); setChatCsrfToken(bootstrap.csrf_token);
  if (!bootstrap.vapid_public_key) throw new Error('通知サーバーが未設定です。');
  const registration = await navigator.serviceWorker.register(bootstrap.sw_url, {scope:'/'});
  const current = await registration.pushManager.getSubscription();
  const subscription = current || await registration.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:applicationServerKey(bootstrap.vapid_public_key)});
  await portalApi.chatPushSubscribe(subscription.toJSON(), registration.scope ? new URL(registration.scope).pathname : '/');
}

export async function disableChatPush(): Promise<void> {
  const registration = await navigator.serviceWorker.getRegistration('/');
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) return;
  await portalApi.chatPushUnsubscribe(subscription.endpoint);
  await subscription.unsubscribe();
}
