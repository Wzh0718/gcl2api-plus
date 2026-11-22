const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    const body = source.slice(source.indexOf('async function loadCallMonitor('), source.indexOf('function _cmUpdateChrome('));
    const requests = [];
    const rendered = [];
    const content = {innerHTML: ''};
    const context = vm.createContext({
        callMonitorState: {view: 'account', range: 'today', failedOnly: false, lastData: null},
        document: {getElementById: () => content},
        URLSearchParams, AbortController, setTimeout, clearTimeout,
        getAuthHeaders: () => ({}),
        closeCallMonitorLatency: () => {},
        _cmEscape: value => String(value),
        renderCallMonitor: () => rendered.push(context.callMonitorState.lastData.marker),
        fetch: (url, options) => new Promise((resolve, reject) => {
            const request = {url, options, reject, resolve: marker => resolve({ok: true, json: async () => ({marker})})};
            // Deliver even cancelled requests to exercise the stale-response guard.
            requests.push(request);
        }),
    });
    vm.runInContext(body, context);
    return {context, requests, rendered, content};
}

test('repeated refresh does not overlap the same monitor query', async () => {
    const {context, requests} = setup();
    const first = context.loadCallMonitor();
    const second = context.loadCallMonitor({silent: true});
    // Resolve all requests even on the old implementation so the test never hangs.
    const count = requests.length;
    requests.forEach(request => request.resolve('result'));
    await Promise.all([first, second]);
    assert.equal(count, 1);
});

test('switching views cancels old query and stale results cannot replace new data', async () => {
    const {context, requests, rendered} = setup();
    const first = context.loadCallMonitor();
    context.callMonitorState.view = 'key';
    const second = context.loadCallMonitor();
    requests[1].resolve('new-key-data');
    await second;
    requests[0].resolve('stale-account-data');
    await first;
    assert.equal(requests[0].options.signal?.aborted, true);
    assert.equal(context.callMonitorState.lastData.marker, 'new-key-data');
    assert.deepEqual(rendered, ['new-key-data']);
});

test('late failure after changing filters leaves the new view intact', async () => {
    const {context, requests, rendered, content} = setup();
    const first = context.loadCallMonitor();
    context.callMonitorState.failedOnly = true;
    const second = context.loadCallMonitor();
    requests[1].resolve('filtered');
    await second;
    const html = content.innerHTML;
    requests[0].reject(new Error('stale error'));
    await first;
    assert.deepEqual(rendered, ['filtered']);
    assert.equal(content.innerHTML, html);
});

function latencyCell(row) {
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    const start = source.indexOf('function _cmFormatLatency(');
    const end = source.indexOf('let callMonitorLatencyTooltip', start);
    const context = vm.createContext({_cmEscape: String});
    vm.runInContext(source.slice(start, end), context);
    return context._cmLatencyCell(row);
}

test('slow response distinguishes time before, between and after upstream calls', () => {
    const html = latencyCell({
        upstream_response_seconds: 3.74, downstream_first_byte_seconds: 28.24,
        timing_details: {stages: {first_upstream_send: 2, last_upstream_send: 12, last_upstream_data: 15.74}},
    });
    assert.match(html, /首次调用上游前 2\.00s/);
    assert.match(html, /首次至末次上游发起 10\.00s/);
    assert.match(html, /上游末包至下游首包 12\.50s/);
});

test('single upstream attempt and overlapping streaming do not invent waits', () => {
    const html = latencyCell({
        upstream_response_seconds: 7, downstream_first_byte_seconds: 1.5,
        timing_details: {stages: {first_upstream_send: 1, last_upstream_send: 1.00001, last_upstream_data: 8}},
    });
    assert.match(html, /首次调用上游前 1\.00s/);
    assert.doesNotMatch(html, /首次至末次|上游末包至下游首包/);
});

test('legacy records without stage timing retain the original metrics', () => {
    const html = latencyCell({response_total_seconds: 28.24, downstream_first_byte_seconds: 28.24});
    assert.match(html, /响应等待 28\.24s/);
    assert.doesNotMatch(html, /首次调用上游前|首次至末次|上游末包至下游首包/);
});

test('settlement tooltip renders only measured phases and converts milliseconds once', () => {
    const html = latencyCell({timing_details: {phase_ms: {
        credential_save_pg_pool: 1250, model_stats_pg_sql: 0, billing_price_lookup: 3,
    }}});
    assert.match(html, /凭证保存等连接 1\.25s/);
    assert.match(html, /模型统计 SQL 0\.00ms/);
    assert.match(html, /计费取价 3\.00ms/);
    assert.doesNotMatch(html, /凭证保存 SQL|模型统计等连接|计费 SQL/);
});

test('automatic refresh only queries the bounded recent window', () => {
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    const body = source.slice(source.indexOf('function startCallMonitorAutoRefresh('), source.indexOf('function _escapeStatsHtml('));
    const requests = [];
    let tick;
    const context = vm.createContext({
        document: {visibilityState: 'visible', getElementById: () => ({classList: {contains: () => true}})},
        AppState: {}, callMonitorState: {autoRefresh: true},
        stopCallMonitorAutoRefresh() {}, setInterval(callback) { tick = callback; },
        loadCallMonitor() { requests.push(context.callMonitorState.range); },
    });
    vm.runInContext(body, context);
    context.startCallMonitorAutoRefresh();
    for (const range of ['today', '7d', '30d', 'all', 'recent']) {
        context.callMonitorState.range = range;
        tick();
    }
    assert.deepEqual(requests, ['recent']);
});
