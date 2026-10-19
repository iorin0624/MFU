const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(
  path.join(__dirname, '..', 'templates', 'view_access_token.html'),
  'utf8',
);
const script = template.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'access-token page script exists');

function openAccessPage(search, hash) {
  const submitted = { count: 0, token: '' };
  const status = { textContent: '' };
  const spinner = { style: {} };
  const tokenInput = { value: '' };
  const form = { submit() { submitted.count += 1; submitted.token = tokenInput.value; } };
  let cleanedUrl = null;
  const elements = {
    statusText: status,
    spinner,
    accessToken: tokenInput,
    tokenForm: form,
  };
  vm.runInNewContext(script, {
    URLSearchParams,
    window: { location: { search, hash, pathname: '/view/example/access' } },
    history: { replaceState(_state, _title, url) { cleanedUrl = url; } },
    document: { getElementById(id) { return elements[id]; } },
  });
  return { submitted, cleanedUrl, status, spinner };
}

for (const [search, hash] of [
  ['?token=mfu_view_new', ''],
  ['', '#mfu_view_legacy'],
]) {
  const result = openAccessPage(search, hash);
  assert.equal(result.submitted.count, 1);
  assert.ok(result.submitted.token.startsWith('mfu_view_'));
  assert.equal(result.cleanedUrl, '/view/example/access');
}

const missing = openAccessPage('', '');
assert.equal(missing.submitted.count, 0);
assert.equal(missing.cleanedUrl, '/view/example/access');

console.log('3 access-token page tests passed');
