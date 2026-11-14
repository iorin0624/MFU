const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../templates/admin_nodes_chrony.html'), 'utf8');
const match = html.match(/function jihoTonesForSecond\(second\) \{[\s\S]*?\n  \}/);
assert.ok(match, 'time-signal tone function is present');
const tonesForSecond = new Function(`${match[0]}; return jihoTonesForSecond;`)();

for (const second of [27, 28, 29, 57, 58, 59]) {
  const [advance, tick] = tonesForSecond(second);
  assert.equal(advance.frequency, 440);
  assert.equal(advance.duration, 0.1);
  assert.equal(tick.frequency, 2000);
  assert.equal(tick.duration, 0.05);
}
for (const second of [0, 30]) {
  const [tone] = tonesForSecond(second);
  assert.equal(tone.frequency, 880);
  assert.equal(tone.duration, 2);
  assert.equal(tone.envelope, 'decay');
}
for (const second of [10, 20, 40, 50]) {
  const [tone] = tonesForSecond(second);
  assert.equal(tone.frequency, 1000);
  assert.equal(tone.duration, 1.5);
}
for (const second of [1, 26, 31, 56]) {
  const [tick] = tonesForSecond(second);
  assert.equal(tick.frequency, 2000);
  assert.equal(tick.duration, 0.05);
}
console.log('chrony time-signal tones: ok');
