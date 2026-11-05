const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'templates', 'base.html'), 'utf8');
const script = source.match(/<script>\s*(\(function\(\)\{\s*const root = document\.getElementById\('mfuNotifRoot'\);[\s\S]*?\}\)\(\);)\s*<\/script>/);
assert.ok(script, 'notification header script is present');

const documentEvents = new Map();
const windowEvents = new Map();
const requests = [];
const timers = new Map();
const socketEvents = new Map();
let timerId = 0;
let connects = 0;
let disconnects = 0;
let socketOptions;

function element() {
  return {
    classList: { add() {}, remove() {} },
    setAttribute() {},
    addEventListener() {},
    contains() { return false; },
  };
}

const elements = Object.fromEntries([
  'mfuNotifRoot', 'mfuNotifBell', 'mfuNotifBadge', 'mfuNotifPopup',
  'mfuNotifList', 'mfuNotifReadAll',
].map((id) => [id, element()]));

const socket = {
  connected: false,
  on(event, handler) { socketEvents.set(event, handler); },
  connect() { connects += 1; this.connected = true; },
  disconnect() { disconnects += 1; this.connected = false; },
};
const document = {
  hidden: true,
  getElementById(id) { return elements[id]; },
  addEventListener(event, handler) { documentEvents.set(event, handler); },
};
const window = {
  io(options) { socketOptions = options; return socket; },
  addEventListener(event, handler) { windowEvents.set(event, handler); },
};

vm.runInNewContext(script[1], {
  document,
  window,
  URL,
  fetch(url) {
    requests.push(url);
    return Promise.resolve({ok: true, json: async () => ({unread_count: 2})});
  },
  setInterval(handler) { const id = ++timerId; timers.set(id, handler); return id; },
  clearInterval(id) { timers.delete(id); },
});

assert.equal(socketOptions.autoConnect, false);
assert.equal(socketOptions.forceNew, true, 'notification-only lifecycle must not disable admin sockets');
assert.equal(connects, 0);
assert.equal(requests.length, 0);
assert.equal(timers.size, 0);

document.hidden = false;
documentEvents.get('visibilitychange')();
assert.equal(connects, 1);
assert.equal(requests.length, 1);
assert.equal(timers.size, 1);

windowEvents.get('pageshow')();
assert.equal(connects, 1, 'pageshow does not create another connection');
assert.equal(requests.length, 1, 'pageshow does not duplicate the initial fetch');

document.hidden = true;
documentEvents.get('visibilitychange')();
assert.equal(disconnects, 1);
assert.equal(timers.size, 0);
socketEvents.get('notif_new')();
assert.equal(requests.length, 1, 'hidden tab ignores notifications');

document.hidden = false;
documentEvents.get('visibilitychange')();
assert.equal(connects, 2);
assert.equal(requests.length, 2, 'returning tab resynchronizes unread count');
assert.equal(timers.size, 1);

windowEvents.get('pagehide')();
assert.equal(disconnects, 2);
assert.equal(timers.size, 0);
