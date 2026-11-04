const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('external_login_user/notifications.py', 'utf8');
const script = source.split('script = r"""', 2)[1].split('"""', 1)[0];
const handlers = {};
let notification;
let opened;
let navigated;
let focused = false;

const app = {
  url: 'https://mfu.iori0624.jp/mfu-notifications/',
  async navigate(url) { navigated = url; return this; },
  async focus() { focused = true; },
};
const clients = {
  async matchAll() { return [app]; },
  async openWindow(url) { opened = url; },
  async claim() {},
};
const context = {
  URL,
  Date,
  Promise,
  self: {
    location: {origin: 'https://mfu.iori0624.jp'},
    navigator: {},
    registration: {
      async showNotification(_title, options) { notification = options; },
    },
    addEventListener(name, handler) { handlers[name] = handler; },
    skipWaiting() {},
  },
  clients,
  async fetch() { return {ok: false}; },
};
vm.runInNewContext(script, context);

async function main() {
  let pending;
  handlers.push({
    data: {json: () => ({
      notification_id: 123,
      target_url: 'https://mfu.iori0624.jp/admin/private-detail',
    })},
    waitUntil(promise) { pending = promise; },
  });
  await pending;
  assert.equal(notification.data.notification_id, 123);

  handlers.notificationclick({
    notification: {data: notification.data, close() {}},
    waitUntil(promise) { pending = promise; },
  });
  await pending;
  assert.equal(navigated, 'https://mfu.iori0624.jp/mfu-notifications/?notification_id=123');
  assert.equal(focused, true);
  assert.equal(opened, undefined);

  clients.matchAll = async () => [];
  handlers.notificationclick({
    notification: {data: {notification_id: 456}, close() {}},
    waitUntil(promise) { pending = promise; },
  });
  await pending;
  assert.equal(opened, 'https://mfu.iori0624.jp/mfu-notifications/?notification_id=456');
  console.log('PWA notification navigation OK');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
