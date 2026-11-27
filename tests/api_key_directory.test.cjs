const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../front/common.js'), 'utf8');
function setup(extra = {}) {
    const ctx = vm.createContext({AppState: {apiKeyAccounts: [{filename:'account.json',email:'alice@example.com'}]}, ...extra});
    vm.runInContext(source.slice(source.indexOf('function apiKeyMatches('), source.indexOf('function updateApiKeySelection(')), ctx);
    return ctx;
}
test('search supports terms, prefixes, account emails, models and effective status', () => {
    const ctx = setup();
    const item = {name:'研发团队', key_prefix:'AbC', claude_credential_name:'account.json', allowed_model:'gemini-pro', effective_status:'expired'};
    for (const query of ['研发', 'GCLI_ABC', 'alice@EXAMPLE.com', '研发 gemini', 'account.json']) assert.ok(ctx.apiKeyMatches(item, query, ''));
    assert.ok(ctx.apiKeyMatches(item, '  ', 'expired'));
    assert.equal(ctx.apiKeyMatches(item, '研发', 'active'), false);
    assert.equal(ctx.apiKeyMatches(item, 'missing', ''), false);
});
test('loads beyond the first 100 keys and deduplicates overlapping pages', async () => {
    const urls = [];
    const ctx = setup({getAuthHeaders:()=>({}), readJsonResponse:async r=>r, fetch:async url=>{
        urls.push(url);
        return urls.length === 1 ? {items:Array.from({length:100},(_,i)=>({id:String(i)})),total:102} : {items:[{id:'99'},{id:'101',name:'末页密钥'}],total:102};
    }});
    const result = await ctx.fetchAllApiKeys();
    assert.equal(urls.length, 2);
    assert.match(urls[1], /page=2$/);
    assert.equal(result.items.length, 101);
    assert.equal(result.items.at(-1).name, '末页密钥');
});
test('page failure rejects incomplete search data', async () => {
    const ctx = setup({getAuthHeaders:()=>({}), readJsonResponse:async r=>r, fetch:async url=>{
        if (url.endsWith('page=2')) throw Error('offline');
        return {items:Array.from({length:100},(_,i)=>({id:i})),total:101};
    }});
    await assert.rejects(ctx.fetchAllApiKeys(), /offline/);
});

test('full-key lookup ignores stale replies after the search is cleared', async () => {
    const timers = [], requests = [], views = [];
    const search = {value:'gcli_12345678_first-secret'};
    const ctx = vm.createContext({
        AppState: {apiKeyAccounts: []}, AbortController,
        document: {getElementById: id => id === 'apiKeySearch' ? search : {value:''}},
        clearTimeout:()=>{}, setTimeout:fn=>{timers.push(fn);return timers.length;},
        getAuthHeaders:()=>({}), readJsonResponse:async r=>r,
        fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),
    });
    vm.runInContext(source.slice(source.indexOf('function toggleApiKeyPanel('),source.indexOf('async function fetchAllApiKeys(')),ctx);
    ctx.applyApiKeyFilter = (matches,message)=>views.push({matched:matches({id:'first',name:'demo'}),message});
    ctx.filterApiKeys();
    const pending = timers.shift()();
    assert.equal(requests[0].options.method,'POST');
    assert.equal(requests[0].url,'./api-keys/lookup');
    search.value = '';
    ctx.filterApiKeys();
    assert.equal(requests[0].options.signal.aborted,true);
    const count=views.length;
    requests[0].resolve({id:'first'});
    await pending;
    assert.equal(views.length,count);
    assert.equal(views.at(-1).matched,true);
});
