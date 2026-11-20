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
        _cmEscape: value => String(value),
        renderCallMonitor: () => rendered.push(context.callMonitorState.lastData.marker),
        fetch: (url, options) => new Promise((resolve, reject) => {
            const request = {url, options, resolve: marker => resolve({ok: true, json: async () => ({marker})})};
            options.signal?.addEventListener('abort', () => {
                const error = new Error('Aborted'); error.name = 'AbortError'; reject(error);
            });
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
