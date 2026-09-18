// Regression tests for kanjieater/gsm-stream#6.
//
// The guard must never drop or rewrite a `text_v2_snapshot` message -- GSM's
// own handler always has to run so its session bookkeeping (current session
// id, removed-line-id tracking) stays correct. It should only suppress the
// one resulting localStorage write that is actually wrong: persisting an
// empty line list when real lines existed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const repoRoot = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const headerUiSrc = readFileSync(path.join(repoRoot, 'header_ui.js'), 'utf8');

function extractGuard(src) {
  const start = src.indexOf('// === textfeed-reconnect-guard:start ===');
  const end = src.indexOf('// === textfeed-reconnect-guard:end ===');
  assert.notEqual(start, -1, 'guard start marker not found in header_ui.js');
  assert.notEqual(end, -1, 'guard end marker not found in header_ui.js');
  return src.slice(start, end);
}

const guardSrc = extractGuard(headerUiSrc);

const LINE_DATA_KEY = 'bannou-texthooker-lineData';
const REMOVED_IDS_KEY = 'bannou-texthooker-removedGSMLineIds';

// Minimal WebSocket/Storage stand-ins exposing `onmessage` and `setItem` as
// prototype members the guard's Object.getOwnPropertyDescriptor lookups and
// monkeypatches depend on, matching the real browser contracts GSM and the
// guard both rely on. These classes are defined in this realm and shared
// into the vm context so the guard's patches (applied inside the vm) mutate
// the same prototype objects this file's fake app uses outside it.
class FakeWebSocket {
  constructor() { this._onmessage = null; }
  dispatch(data) { if (this._onmessage) this._onmessage({ data }); }
}
Object.defineProperty(FakeWebSocket.prototype, 'onmessage', {
  configurable: true,
  enumerable: true,
  get() { return this._onmessage; },
  set(handler) { this._onmessage = handler; },
});

class FakeStorage {
  constructor() { this._backing = new Map(); }
  getItem(k) { return this._backing.has(k) ? this._backing.get(k) : null; }
  setItem(k, v) { this._backing.set(k, String(v)); }
}

function installGuard() {
  const sandbox = { WebSocket: FakeWebSocket, Storage: FakeStorage, console, setTimeout, clearTimeout };
  const localStorage = new FakeStorage();
  sandbox.window = { localStorage };
  vm.createContext(sandbox);
  vm.runInContext(guardSrc, sandbox);
  return { ws: new FakeWebSocket(), localStorage };
}

// Reimplements just enough of GSM's own session-sync pipeline (session-sync.ts,
// reverse-engineered from the shipped bundle) to exercise the real bug and the
// bookkeeping the fix must preserve: current-session tracking (`A`),
// per-session removed-line-id tracking (`Vt`/`N`/`et`/`tt`), and the plan
// builder (`Ju`) whose first loop excludes ids already recorded as removed.
function makeFakeGsmApp(localStorage) {
  const mu = { value: [] };

  // Matches the observed real-world result (confirmed against the live app):
  // blocking the localStorage write also prevents the live value from
  // flipping to empty, because the persisted store's `set` re-derives its
  // in-memory value from what's actually in storage rather than trusting the
  // caller's value directly.
  function setLineData(value) {
    localStorage.setItem(LINE_DATA_KEY, JSON.stringify(value));
    mu.value = JSON.parse(localStorage.getItem(LINE_DATA_KEY));
  }

  function seedLineData(lines) {
    mu.value = lines;
    localStorage.setItem(LINE_DATA_KEY, JSON.stringify(lines));
  }

  let currentSessionId = null; // `A`
  let removedIdsSessionId = null; // `et`
  let removedIds = new Set(); // `tt`

  function persistRemovedIds() {
    localStorage.setItem(REMOVED_IDS_KEY, JSON.stringify([...removedIds]));
  }

  function onSessionObserved(sessionId) { // `Vt`
    if (removedIdsSessionId !== sessionId) {
      removedIdsSessionId = sessionId;
      removedIds = new Set();
      persistRemovedIds();
    }
  }

  function recordRemoved(lines) { // `N`
    if (!currentSessionId) return;
    let changed = false;
    for (const line of lines) {
      if (line.gsmSessionId === currentSessionId && !removedIds.has(line.id)) {
        removedIds.add(line.id);
        changed = true;
      }
    }
    if (changed) persistRemovedIds();
  }

  function buildSyncPlan(evt, currentLines) { // `Ju`
    const orderedIds = new Set(evt.orderedIds);
    const requestedIds = new Set(evt.requestedIds);
    const byId = new Map(currentLines.map((l) => [l.id, l]));
    const synced = [];
    for (const id of new Set(evt.orderedIds)) {
      if (removedIds.has(id)) continue;
      synced.push(byId.get(id) || { id, gsmSessionId: evt.sessionId, text: '(from server)' });
    }
    const retained = [];
    for (const line of currentLines) {
      const consumed = orderedIds.has(line.id) ||
        (line.gsmSessionId === evt.sessionId && requestedIds.has(line.id));
      if (!consumed) retained.push(line);
    }
    return { syncedLines: synced, retainedLines: retained };
  }

  function reconcile(evt) { // `Lt`
    if (!evt.sessionId) return;
    currentSessionId = evt.sessionId;
    onSessionObserved(evt.sessionId);
    const { syncedLines, retainedLines } = buildSyncPlan(evt, mu.value);
    if (!syncedLines.length) {
      // Upstream's buggy branch: fires for every empty snapshot even though
      // `retainedLines` is wrong when `requestedIds` was polluted with every
      // existing same-session id (the actual root cause of #6).
      setLineData(retainedLines);
      return;
    }
    setLineData([...retainedLines, ...syncedLines]);
  }

  // GSM's own `socket.onmessage` handler for this event.
  function handleMessage(event) {
    const msg = JSON.parse(event.data);
    if (msg.event !== 'text_v2_snapshot') return;
    const lines = Array.isArray(msg.lines) ? msg.lines : [];
    reconcile({
      sessionId: msg.session_id,
      orderedIds: lines.map((l) => l.id),
      // The actual upstream bug: computed from every currently-held
      // same-session line instead of only the ones truly requested.
      requestedIds: mu.value.filter((l) => l.gsmSessionId === msg.session_id).map((l) => l.id),
    });
  }

  // gsm-stream's "Reset Lines" header button automates GSM's native "Reset
  // Data" flow: it records the visible same-session lines as removed (so a
  // later resync can't bring them back), then clears the store.
  function resetLines() {
    recordRemoved(mu.value.filter((l) => l.gsmSessionId === currentSessionId));
    setLineData([]);
  }

  return { mu, handleMessage, resetLines, seedLineData, getCurrentSessionId: () => currentSessionId };
}

test('empty snapshot: session bookkeeping still runs, only the destructive write is suppressed', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);
  app.seedLineData([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));

  assert.equal(app.getCurrentSessionId(), 's1', 'current session id must still be recorded');
  assert.equal(
    localStorage.getItem(REMOVED_IDS_KEY),
    JSON.stringify([]),
    'removed-ids bookkeeping for the session must still be initialized',
  );
  assert.deepEqual(app.mu.value, [{ id: 'a', gsmSessionId: 's1', text: 'hello' }]);
  assert.equal(
    localStorage.getItem(LINE_DATA_KEY),
    JSON.stringify([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]),
    'persisted lines must survive the empty snapshot',
  );
});

test('non-empty snapshot still merges normally', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);
  app.seedLineData([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({
    event: 'text_v2_snapshot',
    session_id: 's1',
    lines: [{ id: 'a', gsmSessionId: 's1' }, { id: 'b', gsmSessionId: 's1' }],
  }));

  assert.deepEqual(app.mu.value.map((l) => l.id).sort(), ['a', 'b']);
});

function flushGuardWindow() {
  // The guard's suppression window spans two nested zero-delay timeouts;
  // give it real elapsed macrotasks to clear, matching how far apart these
  // events actually are in a live browser (a user's Reset Lines click always
  // happens well after any reconnect-triggered snapshot has finished
  // processing, not in the same tick).
  return new Promise((resolve) => setTimeout(() => setTimeout(() => setTimeout(resolve, 0), 0), 0));
}

test('reload -> empty snapshot -> Reset Lines -> later non-empty snapshot: removed lines do not return', async () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);

  // Before "reload", the session already had two lines persisted.
  app.seedLineData([
    { id: 'a', gsmSessionId: 's1', text: 'hello' },
    { id: 'b', gsmSessionId: 's1', text: 'world' },
  ]);

  ws.onmessage = app.handleMessage;

  // Reconnect after reload: client is already caught up, server sends an
  // empty snapshot. Without the fix this wipes the lines; with it, both
  // the persisted and live lines must survive.
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));
  assert.deepEqual(app.mu.value.map((l) => l.id).sort(), ['a', 'b'], 'lines must survive the reconnect');

  // User clicks "Reset Lines" sometime later, well after the reconnect's
  // own snapshot processing has finished. This only does the right thing
  // (permanently excludes these ids from future resyncs) if session
  // bookkeeping was kept intact by the fix above.
  await flushGuardWindow();
  app.resetLines();
  assert.deepEqual(app.mu.value, []);
  assert.deepEqual(
    JSON.parse(localStorage.getItem(REMOVED_IDS_KEY)).sort(),
    ['a', 'b'],
    'Reset Lines must have recorded both ids as removed',
  );

  // Server later sends a non-empty snapshot in the same session that still
  // includes the two previously-removed lines plus one genuinely new one.
  ws.dispatch(JSON.stringify({
    event: 'text_v2_snapshot',
    session_id: 's1',
    lines: [
      { id: 'a', gsmSessionId: 's1' },
      { id: 'b', gsmSessionId: 's1' },
      { id: 'c', gsmSessionId: 's1' },
    ],
  }));

  assert.deepEqual(
    app.mu.value.map((l) => l.id),
    ['c'],
    'lines removed via Reset Lines must not reappear from a later resync',
  );
});

test('unrelated events pass through untouched', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);
  app.seedLineData([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]);
  let appendCalls = 0;
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.event === 'text_v2_append') appendCalls += 1;
    else app.handleMessage(event);
  };

  ws.dispatch(JSON.stringify({ event: 'text_v2_append', data: { id: 'c' } }));
  assert.equal(appendCalls, 1);
});

test('malformed message data reaches the handler unchanged (fails open)', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);
  ws.onmessage = app.handleMessage;
  assert.throws(() => ws.dispatch('not json'), /Unexpected token/);
});
