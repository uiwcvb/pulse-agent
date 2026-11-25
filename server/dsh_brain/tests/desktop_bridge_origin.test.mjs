import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectBridgeOrigin } from '../../../desktop/dsh-plugin/lib-local-routes.mjs';

test('inspectBridgeOrigin trusts dsh-app and desktop schemes before Origin.parse', () => {
  for (const origin of [
    'dsh-app://app',
    'dsh-app://shell',
    'dsh-app://local',
    'app://local',
    'file://',
    'tauri://localhost',
    'capacitor://localhost',
    'chrome-extension://abcdef',
    'http://127.0.0.1:3000',
    'http://localhost:5173',
  ]) {
    const info = inspectBridgeOrigin(origin, {});
    assert.equal(info.trusted, true, `expected trusted=true for ${origin}`);
    assert.equal(info.isNonLoopbackWeb, false, `expected isNonLoopbackWeb=false for ${origin}`);
  }

  for (const bad of ['https://evil.example.com', 'http://192.168.1.50:8080', 'null']) {
    const info = inspectBridgeOrigin(bad, {});
    assert.equal(info.trusted, false, `expected trusted=false for ${bad}`);
  }

  // Missing Origin header (non-browser native/CLI clients) remains trusted
  const emptyInfo = inspectBridgeOrigin('', {});
  assert.equal(emptyInfo.trusted, true);
  assert.equal(emptyInfo.origin, null);
});

test('inspectBridgeOrigin rejects opaque http/https while keeping dsh-app trusted when Origin.parse exists', () => {
  const hadOrigin = 'Origin' in globalThis;
  const prevOrigin = globalThis.Origin;
  try {
    globalThis.Origin = {
      parse() {
        return { opaque: true };
      },
    };
    const dshInfo = inspectBridgeOrigin('dsh-app://app', {});
    assert.equal(dshInfo.trusted, true);
    assert.equal(dshInfo.origin, 'dsh-app://app');

    const webInfo = inspectBridgeOrigin('https://evil.example.com', {});
    assert.equal(webInfo.trusted, false);
  } finally {
    if (hadOrigin) {
      globalThis.Origin = prevOrigin;
    } else {
      delete globalThis.Origin;
    }
  }
});
