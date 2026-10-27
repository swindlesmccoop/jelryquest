// Run with node tests/test_jellyfin_ui.cjs (no npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { disabled: true, textContent: '', className: '' });
  return elements.get(id);
};
let now = 1000;
let response = { configured: true, state: 'Running', progress: 87.264 };
let error = false;
const timers = new Map();
const requests = [];
let timerId = 0;
const document = { hidden: false };
const context = vm.createContext({
  $: element, document,
  Date: { now: () => now },
  setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
  clearTimeout: id => timers.delete(id),
  api: async (path, options) => {
    requests.push({ path, options });
    if (error) throw new Error('Connection failed');
    return response;
  },
});
const source = fs.readFileSync('frontend/app.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('let cdnPoll'), source.indexOf('// --- Downloads')), context);
const run = code => vm.runInContext(code, context);
(async () => {
  // Page-load status detects an existing scan and prevents another refresh.
  await run('pollJellyfinStatus()');
  assert.match(element('jellyfin-status').textContent, /87.3%/);
  assert.equal(element('jellyfin-refresh').disabled, true);
  assert.equal([...timers.values()][0].delay, 2000);
  const before = requests.length;
  await run('refreshJellyfin()');
  assert.equal(requests.length, before);
  error = true;
  await run('pollJellyfinStatus()');
  assert.equal(element('jellyfin-refresh').disabled, true);
  assert.equal(timers.size, 1);
  error = false;
  document.hidden = true;
  const hiddenBefore = requests.length;
  await run('pollJellyfinStatus()');
  assert.equal(requests.length, hiddenBefore);
  assert.equal(timers.size, 0);
  document.hidden = false;
  response = { configured: true, state: 'Idle', progress: null };
  await run('pollJellyfinStatus()');
  assert.equal(element('jellyfin-status').textContent, 'Jellyfin scan complete.');
  assert.equal(element('jellyfin-refresh').disabled, false);
  assert.equal(timers.size, 0);
  // A newly requested scan gets time to start instead of immediately claiming completion.
  response = { configured: true, state: 'starting', progress: null };
  await run('refreshJellyfin()');
  assert.match(element('jellyfin-status').textContent, /Waiting/);
  assert.equal(element('jellyfin-refresh').disabled, true);
  response = { configured: true, state: 'Idle', progress: null };
  await run('pollJellyfinStatus()');
  assert.match(element('jellyfin-status').textContent, /Waiting/);
  response = { configured: true, state: 'Running', progress: null };
  await run('pollJellyfinStatus()');
  assert.equal(element('jellyfin-status').textContent, 'Scanning Jellyfin…');
  now += 30 * 60 * 1000;
  await run('pollJellyfinStatus()');
  assert.equal(timers.size, 0);
  assert.match(element('jellyfin-status').textContent, /30 minutes/);
  console.log('Jellyfin UI checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
