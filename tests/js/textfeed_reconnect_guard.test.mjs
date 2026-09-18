// Regression tests for kanjieater/gsm-stream#6.
//
// GSM mutates its live lineData$ store before its persistence subscriber
// writes localStorage, so nothing that reacts to the localStorage write can
// prevent the live view from having already been cleared. The guard instead
// rewrites the raw inbound message *before* GSM's handler computes anything
// from it, so GSM's own (unmodified) reconciliation never enters its
// destructive branch in the first place. It must never drop the message or
// touch session_id -- GSM's session bookkeeping (current session id,
// removed-line-id tracking) has to keep running exactly as GSM designed it.
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

// Fresh WebSocket/Storage stand-ins per test, each exposing `onmessage`/
// storage members the guard's Object.getOwnPropertyDescriptor lookup and
// monkeypatch depend on, matching the real browser contract. A fresh class
// per call means each test's guard installation gets a pristine, unpatched
// prototype -- otherwise later tests would double-wrap a prototype already
// patched by an earlier test.
function installGuard() {
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

  const localStorage = new FakeStorage();
  const sandbox = { WebSocket: FakeWebSocket, console, window: { localStorage } };
  vm.createContext(sandbox);
  vm.runInContext(guardSrc, sandbox);
  return { ws: new FakeWebSocket(), localStorage };
}

// Reimplements just enough of GSM's own session-sync pipeline (reverse
// engineered from the shipped bundle) to exercise the real bug and the
// bookkeeping the fix must preserve: current-session tracking, per-session
// removed-line-id tracking (whose first loop excludes ids already recorded
// as removed), and GSM's documented mutation order -- the live store changes
// first, and persistence is a separate, secondary step.
function makeFakeGsmApp(localStorage) {
  const mu = { value: [] };

  function setLineData(value) {
    mu.value = value; // live BehaviorSubject changes first
    localStorage.setItem(LINE_DATA_KEY, JSON.stringify(value)); // persistence second
  }

  function seedLineData(lines) {
    setLineData(lines);
  }

  let currentSessionId = null;
  let removedIdsSessionId = null;
  let removedIds = new Set();

  function persistRemovedIds() {
    localStorage.setItem(REMOVED_IDS_KEY, JSON.stringify([...removedIds]));
  }

  function onSessionObserved(sessionId) {
    if (removedIdsSessionId !== sessionId) {
      removedIdsSessionId = sessionId;
      removedIds = new Set();
      persistRemovedIds();
    }
  }

  function recordRemoved(lines) {
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

  function buildSyncPlan(evt, currentLines) {
    const orderedIds = new Set(evt.orderedIds);
    const requestedIds = new Set(evt.requestedIds);
    const byId = new Map(currentLines.map((l) => [l.id, l]));
    const synced = [];
    for (const id of new Set(evt.orderedIds)) {
      if (removedIds.has(id)) continue;
      // GSM reads full line content for a requested id from its own
      // still-intact live store when it has it locally.
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

  function reconcile(evt) {
    if (!evt.sessionId) return;
    currentSessionId = evt.sessionId;
    onSessionObserved(evt.sessionId);
    const { syncedLines, retainedLines } = buildSyncPlan(evt, mu.value);
    if (!syncedLines.length) {
      // Upstream's buggy branch: fires whenever nothing was returned, even
      // though `retainedLines` is wrong when `requestedIds` was polluted
      // with every existing same-session id (the actual root cause of #6).
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

  // gsm-stream's "Trash"/"Reset Lines" header button automates GSM's native
  // "Reset Data" flow: it records the visible same-session lines as removed
  // (so a later resync can't bring them back), then clears the store. This
  // never goes through a WebSocket message, so the guard cannot affect it.
  function resetLines() {
    recordRemoved(mu.value.filter((l) => l.gsmSessionId === currentSessionId));
    setLineData([]);
  }

  return { mu, localStorage, handleMessage, resetLines, seedLineData, getCurrentSessionId: () => currentSessionId };
}

test('empty snapshot: live store is never cleared, session bookkeeping still runs', () => {
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
  assert.deepEqual(app.mu.value, [{ id: 'a', gsmSessionId: 's1', text: 'hello' }], 'live store must never go empty');
  assert.equal(
    localStorage.getItem(LINE_DATA_KEY),
    JSON.stringify([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]),
    'persisted lines must survive the empty snapshot',
  );
});

test('empty snapshot with no local history is left untouched', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));

  assert.equal(app.getCurrentSessionId(), 's1');
  assert.deepEqual(app.mu.value, []);
});

test('reconnect preserves lines and new lines still append afterwards', () => {
  const { ws, localStorage } = installGuard();
  const app = makeFakeGsmApp(localStorage);
  app.seedLineData([{ id: 'a', gsmSessionId: 's1', text: 'hello' }]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));
  assert.deepEqual(app.mu.value.map((l) => l.id), ['a'], 'existing line must survive the reconnect');

  // A later, genuinely non-empty snapshot must still merge in new content
  // normally -- the guard must not interfere with real snapshots at all.
  ws.dispatch(JSON.stringify({
    event: 'text_v2_snapshot',
    session_id: 's1',
    lines: [{ id: 'a', gsmSessionId: 's1' }, { id: 'b', gsmSessionId: 's1' }],
  }));
  assert.deepEqual(app.mu.value.map((l) => l.id).sort(), ['a', 'b'], 'new lines must still append');
});

test('reload -> empty snapshot -> Trash/Reset Lines -> later non-empty snapshot: removed lines do not return', () => {
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
  // the live and persisted lines must survive.
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));
  assert.deepEqual(app.mu.value.map((l) => l.id).sort(), ['a', 'b'], 'lines must survive the reconnect');

  // User clicks Trash/Reset Lines. This only does the right thing
  // (permanently excludes these ids from future resyncs) if session
  // bookkeeping was kept intact by the fix above.
  app.resetLines();
  assert.deepEqual(app.mu.value, [], 'trash must clear the live store');
  assert.equal(
    localStorage.getItem(LINE_DATA_KEY),
    JSON.stringify([]),
    'trash must clear the persisted store',
  );
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
    'lines removed via Trash/Reset Lines must not reappear from a later resync',
  );
});

test('unrelated events pass through untouched', () => {
  const { ws } = installGuard();
  let appendCalls = 0;
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.event === 'text_v2_append') appendCalls += 1;
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
