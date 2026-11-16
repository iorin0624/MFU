const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '..', 'static/js/login.js'), 'utf8');

function setup(usernameValue = 'admin') {
  const listeners = new Map();
  const requests = [];
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: '', textContent: '', dataset: {},
        classList: { toggle() {}, add() {}, remove() {} },
        addEventListener(event, listener) { listeners.set(id + ':' + event, listener); },
      });
    }
    return elements.get(id);
  };
  element('login-form').dataset = {
    preauthActive: 'false', adminMfa: 'false', qrLoginEnabled: 'false', preauthUsername: '',
  };
  element('csrf-token').value = 'test-csrf';
  element('username').value = usernameValue;
  const location = { href: '' };
  let response = async (url) => ({
    ok: true,
    json: async () => url.endsWith('/options') ? { challenge: 'test' } : { redirect: '/upload' },
  });
  vm.runInNewContext(script, {
    document: { getElementById: element },
    window: { PublicKeyCredential: function () {} },
    navigator: { credentials: { get: async () => ({ id: 'test-credential' }) } },
    PasskeyUtils: {
      decodeRequestOptions: value => value,
      serializeAuthenticationCredential: value => value,
    },
    location,
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return response(url);
    },
  });
  return { listeners, requests, elements, location, setResponse: fn => { response = fn; } };
}

(async () => {
  const login = setup('admin');
  await login.listeners.get('initial-passkey:click')();
  assert.equal(login.requests.length, 2);
  assert.equal(login.requests[0].body.discoverable, true);
  assert.equal(login.requests[0].body.username, undefined);
  assert.equal(login.requests[1].body.discoverable, true);
  assert.equal(login.requests[1].body.username, undefined);
  assert.equal(login.location.href, '/upload');

  const oldLogin = setup('alice');
  await oldLogin.listeners.get('named-passkey:click')();
  assert.equal(oldLogin.requests[0].body.username, 'alice');
  assert.equal(oldLogin.requests[0].body.discoverable, undefined);

  const failed = setup('admin');
  failed.setResponse(async () => ({
    ok: false, status: 401, json: async () => { throw new Error('non-JSON'); },
  }));
  await failed.listeners.get('initial-passkey:click')();
  assert.match(failed.elements.get('auth-error').textContent, /HTTP 401/);

  console.log('login passkey button regression tests: OK');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
