const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'admin_timer.html'), 'utf8');
const janValidator = template.split('// JAN は 8桁(EAN-8) または 13桁(EAN-13) の数字を許可\n')[1]
  .split('const FOPT', 1)[0];
const janHelpers = 'let janRegistrationDirty = false;' + template.split('let janRegistrationDirty = false;')[1]
  .split('async function reloadJAN()', 1)[0];
const scanHelpers = 'let _lastScanSig =' + template.split('let _lastScanSig =')[1]
  .split('async function loadScanStatus()', 1)[0];

function makePage() {
  const elements = new Map();
  for (const id of [
    'mjan-code', 'mjan-label', 'mjan-minutes', 'mjan-msg', 'mjan-scan-pending',
    'mjan-scan-pending-code', 'mjan-scan-transfer', 'scan-google-search',
    'scan-raw', 'scan-kind', 'scan-ts', 'scan-note',
  ]) {
    const classes = new Set(id === 'mjan-scan-pending' ? ['d-none'] : []);
    elements.set(id, {
      value: '', textContent: '', disabled: id === 'scan-google-search', listeners: {},
      classList: {
        add: name => classes.add(name),
        remove: name => classes.delete(name),
        contains: name => classes.has(name),
      },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      focus() { this.focused = true; },
      click() { this.listeners.click?.(); },
      input() { this.listeners.input?.(); },
    });
  }
  const opened = [];
  const context = vm.createContext({
    document: { querySelector: selector => elements.get(selector.slice(1)) },
    window: { open: (...args) => opened.push(args) },
    encodeURIComponent,
    URL,
  });
  vm.runInContext('const $ = selector => document.querySelector(selector);\n' + janValidator + janHelpers + scanHelpers, context);
  return { context, elements, opened, get: id => elements.get(id) };
}

test('Google search uses only the displayed scan text and requires a click', () => {
  assert.match(template, /id="scan-google-search" disabled/);
  const page = makePage();
  page.context.applyScanStatus({ last_scan: { raw: 'カップ麺 4901234567890', kind: 'OTHER', ts: 1 } });
  assert.equal(page.get('scan-google-search').disabled, false);
  assert.equal(page.opened.length, 0);
  page.get('scan-google-search').click();
  assert.deepEqual(page.opened[0], [
    'https://www.google.com/search?q=' + encodeURIComponent('カップ麺 4901234567890'),
    '_blank', 'noopener,noreferrer',
  ]);
  assert.equal(page.get('mjan-code').value, '');
});

test('HTTP URL scan keeps punctuation and case and opens the URL on click', () => {
  const page = makePage();
  const raw = 'https://www.webq.jp/orca/936537937/OnKkUz?token=a%2Fb#part';
  page.context.applyScanStatus({ last_scan: { raw, kind: 'URL', decoder_version: 'jp106-v2', ts: 1 } });
  assert.equal(page.get('scan-raw').textContent, raw);
  assert.equal(page.get('scan-kind').textContent, 'URL');
  assert.equal(page.get('scan-google-search').textContent, 'URLに飛ぶ');
  assert.equal(page.opened.length, 0);
  page.get('scan-google-search').click();
  assert.deepEqual(page.opened[0], [raw, '_blank', 'noopener,noreferrer']);
});

test('old Japanese-layout scan requires a rescan instead of guessing a URL', () => {
  const page = makePage();
  const raw = "https'//www.example.com/O'Brien?next=1";
  page.context.applyScanStatus({ last_scan: { raw, kind: 'OTHER', ts: 1 } });
  assert.equal(page.get('scan-raw').textContent, raw);
  assert.equal(page.get('scan-google-search').textContent, '再スキャンが必要');
  assert.equal(page.get('scan-google-search').disabled, true);
  page.get('scan-google-search').click();
  assert.equal(page.opened.length, 0);
});

test('plausible but unversioned old URLs are not opened', () => {
  const page = makePage();
  page.context.applyScanStatus({ last_scan: { raw: 'https://example.jp/a_b', kind: 'URL', ts: 1 } });
  assert.equal(page.get('scan-google-search').textContent, '再スキャンが必要');
  assert.equal(page.get('scan-google-search').disabled, true);
});

test('non-HTTP or malformed URL is never opened directly', () => {
  const page = makePage();
  page.context.applyScanStatus({ last_scan: { raw: 'javascript:alert(1)', kind: 'OTHER', ts: 1 } });
  assert.equal(page.get('scan-google-search').textContent, 'Googleで検索');
  page.get('scan-google-search').click();
  assert.equal(page.opened[0][0], 'https://www.google.com/search?q=javascript%3Aalert(1)');
});

test('a new valid JAN fills only the registration form without saving', () => {
  const page = makePage();
  page.context.applyScanStatus({ last_scan: { raw: '4901234567890', kind: 'JAN', ts: 1 } });
  assert.equal(page.get('mjan-code').value, '', 'the last scan at page load is only a baseline');
  page.context.applyScanStatus({ last_scan: { raw: '12345670', kind: 'JAN', ts: 2 } });
  assert.equal(page.get('mjan-code').value, '12345670');
  assert.equal(page.get('mjan-label').value, '');
  assert.equal(page.get('mjan-minutes').value, '');
  assert.equal(page.get('mjan-scan-pending').classList.contains('d-none'), true);
  page.context.applyScanStatus({ last_scan: { raw: '4901234567890', kind: 'JAN', ts: 1 } });
  assert.equal(page.get('mjan-code').value, '12345670', 'older status must not replace the newer scan');
  page.context.applyScanStatus({ last_scan: { raw: '123456789012', kind: 'JAN', ts: 3 } });
  assert.equal(page.get('mjan-code').value, '12345670', 'invalid JAN is ignored');
});

test('manual edits are preserved until the user explicitly transfers a new JAN', () => {
  const page = makePage();
  page.context.applyScanStatus({ last_scan: { raw: '', kind: '', ts: 0 } });
  page.get('mjan-code').value = '4901234567890';
  page.get('mjan-label').value = '編集中の商品';
  page.get('mjan-minutes').value = '3';
  page.get('mjan-label').input();
  page.context.applyScanStatus({ last_scan: { raw: '12345670', kind: 'JAN', ts: 2 } });
  assert.equal(page.get('mjan-code').value, '4901234567890');
  assert.equal(page.get('mjan-label').value, '編集中の商品');
  assert.equal(page.get('mjan-scan-pending-code').textContent, '12345670');
  assert.equal(page.get('mjan-scan-pending').classList.contains('d-none'), false);
  page.context.applyScanStatus({ last_scan: { raw: '4901234567890', kind: 'JAN', ts: 3 } });
  assert.equal(page.get('mjan-scan-pending').classList.contains('d-none'), true);
  page.context.applyScanStatus({ last_scan: { raw: '12345670', kind: 'JAN', ts: 4 } });
  page.get('mjan-scan-transfer').click();
  assert.equal(page.get('mjan-code').value, '12345670');
  assert.equal(page.get('mjan-label').value, '');
  assert.equal(page.get('mjan-minutes').value, '');
  assert.equal(page.get('mjan-scan-pending').classList.contains('d-none'), true);
});
