import assert from 'node:assert/strict';
import test from 'node:test';
import { evaluationApi, resolveEvaluationTarget } from './client.js';

test('evaluation adapter preserves validation-array details and field labels', async (t) => {
    const detail = [{loc:['body','name'],msg:'Required'}];
    t.mock.method(globalThis, 'fetch', async () => Response.json({detail},{status:422}));
    await assert.rejects(evaluationApi('/test'), error => {
        assert.equal(error.message,'name: Required');
        assert.equal(error.status,422);
        assert.deepEqual(error.details,detail);
        return true;
    });
});


test('submission resolves the selected model service group id', () => {
    assert.equal(resolveEvaluationTarget({targetId: 'msg-1'}), 'msg-1');
    assert.throws(() => resolveEvaluationTarget({targetId: ''}), /Select an existing model service/);
});
