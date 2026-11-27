const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('retry status triggers masked details while the account column shows the first account', () => {
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    const context = vm.createContext({
        callMonitorState: {mask: false}, CM_ACCOUNT_TIER_HEADER: '',
        ...Object.fromEntries(['_cmTaskLabel', '_cmFormatTokens', '_cmModelCell', '_cmRequestModeCell', '_cmLatencyCell', '_cmFormatTime', '_cmFormatCost', '_cmAccountTierCell', '_cmTelemetryCell'].map(name => [name, () => ''])),
        _cmWarmupBadge: () => 'settled-account-badge',
    });
    for (const name of ['_cmEscape', '_cmMask', '_cmRateLimitText', '_cmStatusCodeChip', '_cmTooltipTrigger', '_cmTextRetryCells', '_cmRequestFeatureCell', '_cmRenderRealtime']) {
        const start = source.indexOf(`function ${name}(`);
        vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
    }
    const retry = {state: 'sent', attempts: [
        {user_email: 'first@example.test', status_code: 429, elapsed_ms: 17, model: '<test>', retry_after: '12', rate_limit: {source: 'upstream', reason: 'quota_exhausted'}},
        {credential_name: 'backup@example.test', status_code: 200, elapsed_ms: 27},
    ]};
    const row = {credential_name: 'backup@example.test', request_features: {text_account_retry: retry}};
    const cells = context._cmTextRetryCells(row);
    assert.equal(cells.account, 'first@example.test');
    const visibleStatus = cells.status.split('查看重试详情">')[1];
    assert.equal(visibleStatus.replace(/<[^>]*>/g, ''), '429 → 200');
    assert.match(cells.status, /data-latency-tip="原账号 · first@example\.test · HTTP 429/);
    assert.match(cells.status, /备选账号 · backup@example\.test · HTTP 200/);
    assert.match(cells.status, /onmouseenter="showCallMonitorLatency\(this\)"/);
    assert.match(cells.status, /onclick="showCallMonitorLatency\(this\)"/);
    const rendered = context._cmRenderRealtime([row]);
    assert.match(rendered, /<td><button[^>]*data-tip-multiline/);
    assert.match(rendered, /<td class="cm-key-cell" title="账号: first@example\.test">first@example\.test<\/td>/);
    assert.doesNotMatch(rendered, /settled-account-badge|title="账号: backup@example\.test"/);
    assert.match(cells.details, /配额耗尽.*Retry-After: 12/);
    assert.match(cells.details, /&lt;test&gt;/);
    retry.attempts.push({credential_name: 'backup@example.test', kind: 'same_account', status_code: 200});
    assert.match(context._cmTextRetryCells(row).details, /原号重试 · backup@example.test · HTTP 200/);
    assert.equal(context._cmTextRetryCells(row).account, 'first@example.test');
    retry.attempts.pop();
    assert.match(context._cmRequestFeatureCell(row, 0), /查看详情/);
    context.callMonitorState.mask = true;
    assert.doesNotMatch(JSON.stringify(context._cmTextRetryCells(row)), /first@example|backup@example/);
    delete retry.attempts[1].status_code;
    assert.match(context._cmTextRetryCells(row).status, /未收到响应/);
    retry.state = 'not_sent';
    assert.match(context._cmTextRetryCells(row).status, /未发送/);
    assert.doesNotMatch(context._cmTextRetryCells(row).account, /→|换号 1 次|备选/);
    retry.state = 'no_candidate';
    retry.attempts.pop();
    assert.match(context._cmTextRetryCells(row).status, /data-latency-tip="无备选账号/);
    assert.equal(context._cmTextRetryCells({}), null);
});

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

test('refresh preserves masked details while enabling masking closes cleartext details', async () => {
    const {context, requests, content} = setup();
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    for (const name of ['renderCallMonitor', 'toggleCallMonitorMask']) {
        const start = source.indexOf(`function ${name}(`);
        vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
    }
    const dialog = {open: true, close() { this.open = false; }, remove() { this.open = false; }};
    context.document.getElementById = id => ({callMonitorContent: content, cmRequestContentDialog: dialog})[id];
    Object.assign(context, {_cmUpdateChrome() {}, _cmEmptyState: () => 'empty', _cmPaginationBar: () => ''});
    Object.assign(context.callMonitorState, {mask: true, view: 'realtime', range: 'recent'});
    for (const silent of [true, false]) {
        const load = context.loadCallMonitor({silent});
        requests.at(-1).resolve('refreshed');
        await load;
        assert.equal(content.innerHTML, 'empty');
        assert.equal(dialog.open, true);
    }
    context.toggleCallMonitorMask();
    assert.equal(context.callMonitorState.mask, false);
    assert.equal(dialog.open, false);
    dialog.open = true;
    context.toggleCallMonitorMask();
    assert.equal(context.callMonitorState.mask, true);
    assert.equal(dialog.open, false);
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

test('ingress timing and event-loop lag are shown when collected', () => {
    const html = latencyCell({
        timing_details: {ingress: {
            body_receive_seconds: 73.58,
            event_loop_lag_max_seconds: 0.12,
        }},
    });
    assert.match(html, /请求体首字节至末字节 73\.58s/);
    assert.match(html, /事件循环最大延迟 120\.00ms/);
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
    context.callMonitorState.range = 'all';
    context.callMonitorState.historyBuilding = true;
    tick();
    context.callMonitorState.historyBuilding = false;
    tick();
    assert.deepEqual(requests, ['recent', 'all']);
});

test('a backfill response keeps historical polling enabled until exact data arrives', async () => {
    const {context, requests, content} = setup();
    context.fetch = async () => ({
        status: 503, headers: {get: () => 'building'},
        json: async () => ({detail: 'backfill'}),
    });
    await context.loadCallMonitor();
    assert.equal(context.callMonitorState.historyBuilding, true);
    assert.match(content.innerHTML, /构建历史汇总/);
    context.fetch = async () => ({ok: true, json: async () => ({marker: 'complete'})});
    await context.loadCallMonitor({silent: true});
    assert.equal(context.callMonitorState.historyBuilding, false);
    assert.equal(context.callMonitorState.lastData.marker, 'complete');
    assert.equal(requests.length, 0);
});
