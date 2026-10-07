import test from 'node:test';
import assert from 'node:assert/strict';
import { monitoringLinkTarget } from './monitoringLinks.js';
import { checkPassed, checkLabel, checkTone } from './preflightPresentation.js';

test('monitoring links preserve advertised dashboard paths and unavailable reasons', () => {
    assert.equal(monitoringLinkTarget({ links: [{ kind: 'grafana', available: true, local_port: 3001, dashboard_path: '/d/gpu' }] }, 'grafana', 'lens.local'), 'http://lens.local:3001/d/gpu');
    assert.throws(() => monitoringLinkTarget({ links: [{ kind: 'grafana', message: 'No service' }] }, 'grafana', 'lens.local'), /No service/);
});
test('preflight keeps advisory failures distinct from blockers', () => {
    assert.equal(checkPassed({ status: 'ready' }), true);
    assert.equal(checkTone({ blocking: false }), 'warning');
    assert.equal(checkLabel({ blocking: false }), 'Missing');
    assert.equal(checkTone({ blocking: true }), 'danger');
});
