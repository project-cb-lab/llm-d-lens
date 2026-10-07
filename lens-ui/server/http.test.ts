import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchJsonWithTimeout } from './http.ts';
import { runWithInternalAuth } from './internalAuth.ts';

test('upstream JSON transport preserves HTTP failures and raw malformed body', async (t) => {
    const responses = [new Response('{"detail":"bad"}', {status: 422}), new Response('upstream down', {status: 502}), new Response('', {status: 200})];
    t.mock.method(globalThis, 'fetch', async () => responses.shift()!);
    const failed = await fetchJsonWithTimeout('http://upstream', {}, 1000);
    assert.equal(failed.response.status, 422);
    assert.deepEqual(failed.payload, {detail: 'bad'});
    assert.deepEqual((await fetchJsonWithTimeout('http://upstream', {}, 1000)).payload, {detail: 'upstream down'});
    assert.deepEqual((await fetchJsonWithTimeout('http://upstream', {}, 1000)).payload, {});
});
test('timeout covers body consumption and is cleared after completion', async (t) => {
    t.mock.timers.enable({apis: ['setTimeout']});
    let signal: AbortSignal;
    const fetchMock = t.mock.method(globalThis, 'fetch', async (_url, init) => {
        signal = init.signal;
        return {text: () => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), {once: true}))};
    });
    const pending = fetchJsonWithTimeout('http://upstream', {}, 20);
    const rejected = assert.rejects(pending, {name: 'AbortError'});
    await Promise.resolve();
    t.mock.timers.tick(20);
    await rejected;
    fetchMock.mock.mockImplementation(async (_url, init) => {signal = init.signal; return new Response('{}');});
    await fetchJsonWithTimeout('http://upstream', {}, 20);
    t.mock.timers.tick(30);
    assert.equal(signal!.aborted, false);
});

test('configuration and candidate routes retain distinct error bodies and status codes', async (t) => {
    const {default: express} = await import('express');
    const {configurationRouter} = await import('./configuration.ts');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), configurationRouter, candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    let upstream: () => Promise<Response> = async () => Response.json({detail: [{msg: 'invalid'}]}, {status: 422});
    t.mock.method(globalThis, 'fetch', () => upstream());
    const post = (route: string) => nativeFetch(`http://127.0.0.1:${address.port}${route}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model'}, searchConfig: {aicSystemName: 'b60'}}),
    });
    const config = await post('/api/configurations/resolve');
    assert.equal(config.status, 422);
    assert.deepEqual(await config.json(), {detail: [{msg: 'invalid'}], error: 'Configuration service returned HTTP 422'});
    const candidate = await post('/api/candidate-support');
    assert.equal(candidate.status, 422);
    assert.deepEqual(await candidate.json(), {error: 'AIC returned HTTP 422'});
    upstream = async () => {throw new DOMException('timeout', 'AbortError');};
    const configTimeout = await post('/api/configurations/render');
    assert.equal(configTimeout.status, 504);
    assert.deepEqual(await configTimeout.json(), {error: 'Configuration service timed out'});
    const candidateTimeout = await post('/api/candidate-support');
    assert.equal(candidateTimeout.status, 504);
    assert.deepEqual(await candidateTimeout.json(), {error: 'AIC candidate search timed out'});
});

test('candidate support forwards the authenticated caller to AIC', async (t) => {
    const previousSecret = process.env.LENS_INTERNAL_AUTH_SECRET;
    process.env.LENS_INTERNAL_AUTH_SECRET = 'candidate-search-test-secret';
    t.after(() => {
        if (previousSecret === undefined) delete process.env.LENS_INTERNAL_AUTH_SECRET;
        else process.env.LENS_INTERNAL_AUTH_SECRET = previousSecret;
    });

    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json());
    app.use((_req, _res, next) => runWithInternalAuth('user-123', next));
    app.use(candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');

    const nativeFetch = globalThis.fetch;
    let internalPrincipal: string | null = null;
    t.mock.method(globalThis, 'fetch', async (_url, init) => {
        internalPrincipal = new Headers(init?.headers).get('x-prism-principal-id');
        return Response.json({supported: true});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-support`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model'}, searchConfig: {aicSystemName: 'b60'}}),
    });

    assert.equal(response.status, 200);
    assert.equal(internalPrincipal, 'user-123');
});

test('manual estimate sends the exact topology GPU count to AIC', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');

    const nativeFetch = globalThis.fetch;
    let estimateBody: Record<string, unknown> = {};
    t.mock.method(globalThis, 'fetch', async (url, init) => {
        if (String(url).endsWith('/estimate')) {
            estimateBody = JSON.parse(String(init?.body));
            return Response.json({mode: 'agg', tp: 2, replicas: 2, ttft_ms: 120, tpot_ms: 20});
        }
        return Response.json({supported: true, agg_supported: true});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            workload: {model: 'Qwen/Qwen3-8B'}, searchConfig: {aicSystemName: 'b60', totalGpus: 8},
            sourceIds: ['manual'], manualConfig: {servingMode: 'agg', tp: 2, replicas: 2},
        }),
    });

    assert.equal(response.status, 200);
    assert.equal(estimateBody.gpu_count, 4);
    assert.equal(estimateBody.tp, 2);
    assert.equal(estimateBody.replicas, 2);
});

test('relaxed AIC search returns a complete anchor after bounded retries', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    const requests: Record<string, unknown>[] = [];
    t.mock.method(globalThis, 'fetch', async (_url, init) => {
        if (String(_url).endsWith('/support')) return Response.json({supported: true});
        const payload = JSON.parse(String(init?.body));
        requests.push(payload);
        return Response.json({configs: requests.length === 1 ? [] : [{
            mode: 'agg', tp: 2, replicas: 4, ttft_ms: 550, tpot_ms: 40,
            throughput_tokens_per_sec: 100,
        }]});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sourceIds: ['aic'], workload: {
            model: 'model', isl: 1024, osl: 256, ttftMs: 500, tpotMs: 30,
        }, searchConfig: {totalGpus: 8, aicSystemName: 'b60'}}),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.deepEqual(requests.map(item => item.ttft_target_ms), [500, 625]);
    assert.deepEqual(requests.map(item => item.tpot_target_ms), [30, 37.5]);
    assert.ok(requests.every(item => item.gpu_count === 8 && item.model_name === 'model'));
    assert.equal(result.attempts.length, 2);
    assert.equal(result.anchor.decodeTp, 2);
    assert.equal(result.anchor.decodeReplicas, 4);
    assert.equal(result.sloSatisfied, false);
});

test('relaxed AIC search continues after no-feasible 400 without changing GPU budget', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    const requests: Record<string, unknown>[] = [];
    t.mock.method(globalThis, 'fetch', async (url, init) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        requests.push(JSON.parse(String(init?.body)));
        if (requests.length === 1) {
            return Response.json({detail: {error: 'No feasible configurations found for the given parameters.'}}, {status: 400});
        }
        return Response.json({configs: [{mode: 'agg', tp: 1, replicas: 3, ttft_ms: 44, tpot_ms: 9, throughput_tokens_per_sec: 100}]});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 40, tpotMs: 10}, searchConfig: {totalGpus: 3}}),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.deepEqual(requests.map(item => item.ttft_target_ms), [40, 50]);
    assert.deepEqual(requests.map(item => item.tpot_target_ms), [10, 12.5]);
    assert.ok(requests.every(item => item.gpu_count === 3));
    assert.equal(result.attempts.length, 2);
    assert.equal(result.anchor.decodeTp, 1);
    assert.equal(result.anchor.decodeReplicas, 3);
    assert.equal(result.sloSatisfied, false);
});

test('relaxed AIC search prefers a smaller topology with better latency', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    t.mock.method(globalThis, 'fetch', async (url) => String(url).endsWith('/support')
        ? Response.json({supported: true})
        : Response.json({configs: [
            {mode: 'agg', tp: 1, replicas: 1, ttft_ms: 41, tpot_ms: 5},
            {mode: 'agg', tp: 1, replicas: 3, ttft_ms: 45, tpot_ms: 9},
        ]}));
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 40, tpotMs: 10}, searchConfig: {totalGpus: 3, maxCandidatesPerMode: 1}}),
    });
    const result = await response.json();
    assert.equal(response.status, 200);
    assert.equal(result.candidates.length, 1);
    assert.equal(result.anchor.totalGpus, 1);
    assert.equal(result.anchor.decodeReplicas, 1);
    assert.equal(result.sloSatisfied, false);
});

test('relaxed AIC search accepts a topology under budget and rejects one over budget', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    let searches = 0;
    t.mock.method(globalThis, 'fetch', async (url) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        searches += 1;
        return Response.json({configs: [
            {mode: 'agg', tp: 1, replicas: 1, ttft_ms: 44, tpot_ms: 9},
            {mode: 'agg', tp: 1, replicas: 4, ttft_ms: 43, tpot_ms: 9},
        ]});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 40, tpotMs: 10}, searchConfig: {totalGpus: 3}}),
    });
    const result = await response.json();
    assert.equal(response.status, 200);
    assert.equal(searches, 1);
    assert.equal(result.candidates.length, 1);
    assert.equal(result.anchor.totalGpus, 1);
    assert.equal(result.reason, null);
});

test('relaxed AIC search retries a string-detail no-feasible 400', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    const requests: Record<string, unknown>[] = [];
    t.mock.method(globalThis, 'fetch', async (url, init) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        requests.push(JSON.parse(String(init?.body)));
        if (requests.length === 1) {
            return Response.json({detail: 'No feasible configurations found for the given parameters.'}, {status: 400});
        }
        return Response.json({configs: [{mode: 'agg', tp: 1, replicas: 3, ttft_ms: 44, tpot_ms: 9, throughput_tokens_per_sec: 100}]});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 40, tpotMs: 10}, searchConfig: {totalGpus: 3}}),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.deepEqual(requests.map(item => item.ttft_target_ms), [40, 50]);
    assert.deepEqual(requests.map(item => item.tpot_target_ms), [10, 12.5]);
    assert.ok(requests.every(item => item.gpu_count === 3));
    assert.equal(result.attempts.length, 2);
    assert.equal(result.anchor.decodeTp, 1);
    assert.equal(result.anchor.decodeReplicas, 3);
    assert.equal(result.sloSatisfied, false);
});

test('relaxed AIC search stops after five no-feasible attempts without fabricating an anchor', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    const attempts: Record<string, unknown>[] = [];
    t.mock.method(globalThis, 'fetch', async (url, init) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        attempts.push(JSON.parse(String(init?.body)));
        return Response.json({detail: {error: 'No feasible configurations found for the given parameters.'}}, {status: 400});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 500, tpotMs: 30}, searchConfig: {totalGpus: 4}}),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.deepEqual(attempts.map(item => item.ttft_target_ms), [500, 625, 750, 875, 1000]);
    assert.deepEqual(attempts.map(item => item.tpot_target_ms), [30, 37.5, 45, 52.5, 60]);
    assert.ok(attempts.every(item => item.gpu_count === 4));
    assert.equal(result.attempts.length, 5);
    assert.equal(result.anchor, null);
    assert.equal(result.reason, 'no_complete_anchor');
});

test('relaxed AIC search propagates unrelated 400 without retrying', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    let searches = 0;
    t.mock.method(globalThis, 'fetch', async (url) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        searches += 1;
        return Response.json({detail: {error: 'Invalid model'}}, {status: 400});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 40}, searchConfig: {totalGpus: 3}}),
    });
    assert.equal(response.status, 400);
    assert.equal(searches, 1);
});

test('relaxed AIC search skips incomplete results before selecting an anchor', async (t) => {
    const {default: express} = await import('express');
    const {candidateSearchRouter} = await import('./candidateSearch.ts');
    const app = express();
    app.use(express.json(), candidateSearchRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.once('listening', resolve));
    t.after(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const nativeFetch = globalThis.fetch;
    let searches = 0;
    t.mock.method(globalThis, 'fetch', async (url) => {
        if (String(url).endsWith('/support')) return Response.json({supported: true});
        searches += 1;
        return Response.json({configs: searches === 1
            ? [{mode: 'agg', tp: 1, ttft_ms: 300, tpot_ms: 20}]
            : [{mode: 'agg', tp: 2, replicas: 4, ttft_ms: 600, tpot_ms: 40}]});
    });
    const response = await nativeFetch(`http://127.0.0.1:${address.port}/api/candidate-search/relaxed`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({workload: {model: 'model', ttftMs: 500, tpotMs: 30}, searchConfig: {totalGpus: 8}}),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(searches, 2);
    assert.equal(result.anchor.decodeTp, 2);
    assert.equal(result.anchor.decodeReplicas, 4);
});
