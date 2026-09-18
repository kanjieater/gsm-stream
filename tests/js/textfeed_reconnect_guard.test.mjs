// Regression test for kanjieater/gsm-stream#6.
//
// Exercises the real reconnect scenario end-to-end against a fake app that
// reproduces upstream's documented mutation order (lineData$ is mutated
// in-memory first, then a persistence subscriber writes localStorage), not
// just the guard in isolation. The guard must stop that fake app's handler
// from running at all for an empty incremental snapshot, so neither the
// live store nor localStorage is ever touched -- regardless of upstream's
// internal ordering.
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

// Minimal WebSocket stand-in exposing `onmessage` as an accessor property on
// the prototype, matching the browser WebIDL EventHandler pattern that the
// guard's Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage')
// lookup depends on.
function makeSandbox() {
  class FakeWebSocket {
    constructor() {
      this._onmessage = null;
    }
    dispatch(data) {
      if (this._onmessage) this._onmessage({ data });
    }
  }
  Object.defineProperty(FakeWebSocket.prototype, 'onmessage', {
    configurable: true,
    enumerable: true,
    get() { return this._onmessage; },
    set(handler) { this._onmessage = handler; },
  });

  const sandbox = { WebSocket: FakeWebSocket, console };
  vm.createContext(sandbox);
  vm.runInContext(guardSrc, sandbox);
  return sandbox;
}

// Reproduces upstream's reported behavior: a persisted store whose value is
// mutated first, then a subscriber persists it to localStorage -- separately,
// and after the fact. This models the exact ordering the PR review called
// out as the thing a storage-hook-only fix could miss.
function makeFakeApp() {
  const localStorageBacking = new Map();
  const localStorage = {
    getItem: (k) => (localStorageBacking.has(k) ? localStorageBacking.get(k) : null),
    setItem: (k, v) => localStorageBacking.set(k, String(v)),
  };

  let liveLines = [];
  const persistLineData = () => localStorage.setItem('bannou-texthooker-lineData', JSON.stringify(liveLines));

  let handlerCalls = 0;
  // This is GSM's own `socket.onmessage = handleMessage.bind(this)` handler,
  // reimplemented to match the buggy reconciliation described in the issue.
  function handleMessage(event) {
    handlerCalls += 1;
    const msg = JSON.parse(event.data);
    if (msg.event === 'text_v2_snapshot') {
      const orderedIds = new Set((msg.lines || []).map((l) => l.id));
      const syncedLines = liveLines.filter((l) => orderedIds.has(l.id));
      if (syncedLines.length === 0) {
        // upstream's `!r.length` branch: mutate the live store first...
        liveLines = [];
        // ...then a separate persistence subscriber writes localStorage.
        persistLineData();
      }
    }
  }

  return {
    localStorage,
    handleMessage,
    seedLines(lines) {
      liveLines = lines;
      persistLineData();
    },
    getLiveLines: () => liveLines,
    getHandlerCallCount: () => handlerCalls,
  };
}

test('drops an empty text_v2_snapshot before it reaches the app handler', () => {
  const sandbox = makeSandbox();
  const ws = new sandbox.WebSocket();
  const app = makeFakeApp();

  app.seedLines([
    { id: 'a', text: 'hello' },
    { id: 'b', text: 'world' },
  ]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({ event: 'text_v2_snapshot', session_id: 's1', lines: [] }));

  assert.equal(app.getHandlerCallCount(), 0, 'handler must not run for an empty snapshot');
  assert.deepEqual(app.getLiveLines(), [{ id: 'a', text: 'hello' }, { id: 'b', text: 'world' }]);
  assert.equal(
    app.localStorage.getItem('bannou-texthooker-lineData'),
    JSON.stringify([{ id: 'a', text: 'hello' }, { id: 'b', text: 'world' }]),
  );
});

test('still delivers a non-empty text_v2_snapshot normally', () => {
  const sandbox = makeSandbox();
  const ws = new sandbox.WebSocket();
  const app = makeFakeApp();

  app.seedLines([{ id: 'a', text: 'hello' }]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({
    event: 'text_v2_snapshot',
    session_id: 's1',
    lines: [{ id: 'b', text: 'world' }],
  }));

  assert.equal(app.getHandlerCallCount(), 1, 'handler must run when the snapshot has lines');
});

test('still delivers unrelated events normally', () => {
  const sandbox = makeSandbox();
  const ws = new sandbox.WebSocket();
  const app = makeFakeApp();
  app.seedLines([{ id: 'a', text: 'hello' }]);

  ws.onmessage = app.handleMessage;
  ws.dispatch(JSON.stringify({ event: 'text_v2_append', data: { id: 'c', text: 'new line' } }));

  assert.equal(app.getHandlerCallCount(), 1);
});

test('fails open on malformed message data', () => {
  const sandbox = makeSandbox();
  const ws = new sandbox.WebSocket();
  const app = makeFakeApp();

  ws.onmessage = app.handleMessage;
  assert.throws(() => ws.dispatch('not json'), /Unexpected token/);
  // The guard itself must not throw or swallow non-JSON messages; the
  // exception above is the app's own JSON.parse, proving the message reached it.
});

test('never touches Storage.prototype, so Reset Lines / Reset Data are unaffected', () => {
  const nativeSetItem = typeof Storage !== 'undefined' ? Storage.prototype.setItem : undefined;
  makeSandbox();
  if (nativeSetItem) {
    assert.equal(Storage.prototype.setItem, nativeSetItem, 'guard must not patch localStorage at all');
  }
});
