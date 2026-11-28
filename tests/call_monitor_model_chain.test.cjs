const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 只切出模型单元格相关的函数：_cmModelServiceCell/_cmEscape 用桩替代。
// 断言关注结构：网关改投是同一行的细箭头，上游路由是执行模型下面的折线分支。
function setup() {
    const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');
    const start = source.indexOf('const CM_MODEL_HOP_LABELS');
    const end = source.indexOf('// 密钥 → 账号视图');
    assert.ok(start > 0 && end > start, 'model cell slice missing');
    const ctx = vm.createContext({
        _cmEscape: value => String(value == null ? '' : value),
        _cmModelServiceCell: (row, html, title) => `${html}||${title}`,
    });
    vm.runInContext(source.slice(start, end), ctx);
    return ctx;
}

function cell(row) {
    return setup()._cmModelCell(row);
}

function count(markup, needle) {
    return markup.split(needle).length - 1;
}

function text(markup) {
    return markup
        .split('||')[0]
        .replace(/<svg[\s\S]*?<\/svg>/g, ' ')
        .replace(/<[^>]*>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

test('gateway hop draws one arrow, upstream routing hangs below as a branch', () => {
    const markup = cell({
        request_model: 'gemini-2.5-flash-lite',
        model_name: 'gemini-3.8-flash-low',
        upstream_model: 'gemini-3.8-flash-n',
        request_features: {
            model_fallback: {
                reason: 'capacity_503',
                events: [{reason: 'capacity_503', from: 'gemini-2.5-flash-lite', to: 'gemini-3.8-flash-low'}],
            },
        },
    });
    assert.equal(count(markup, 'cm-chain-arrow'), 1, 'one gateway hop = one arrow');
    assert.equal(count(markup, 'cm-chain-branch'), 1);
    assert.equal(count(markup, 'cm-chain-up-label'), 1);
    assert.equal(text(markup), 'gemini-2.5-flash-lite gemini-3.8-flash-low 上游 gemini-3.8-flash-n');
    // 分支嵌在执行模型节点里，而不是同一行的兄弟节点
    assert.match(markup, /cm-chain-tail">gemini-3\.8-flash-low<span class="cm-chain-branch"/);
    assert.match(markup.split('||')[1], /网关改投: gemini-2\.5-flash-lite → gemini-3\.8-flash-low（容量 503）/);
});

test('multiple gateway hops chain as multiple arrows before the branch', () => {
    const markup = cell({
        request_model: 'a',
        model_name: 'c',
        upstream_model: 'c-n',
        request_features: {
            model_fallback: {
                reason: 'capacity_503',
                events: [
                    {reason: 'capacity_window', from: 'a', to: 'b'},
                    {reason: 'capacity_503', from: 'b', to: 'c'},
                ],
            },
        },
    });
    assert.equal(count(markup, 'cm-chain-arrow'), 2);
    assert.equal(text(markup), 'a b c 上游 c-n');
    const tip = markup.split('||')[1];
    assert.match(tip, /（容量窗口）/);
    assert.match(tip, /（容量 503）/);
});

test('upstream-only routing shows no gateway arrow', () => {
    const markup = cell({
        request_model: 'gemini-2.5-flash-lite',
        model_name: 'gemini-2.5-flash-lite',
        upstream_model: 'gemini-3.8-flash-n',
    });
    assert.equal(count(markup, 'cm-chain-arrow'), 0, 'upstream routing must not draw a gateway arrow');
    assert.equal(count(markup, 'cm-chain-branch'), 1);
    assert.equal(text(markup), 'gemini-2.5-flash-lite 上游 gemini-3.8-flash-n');
});

test('legacy record without trace still separates gateway hop from upstream branch', () => {
    const markup = cell({
        request_model: 'gemini-2.5-flash-lite',
        model_name: 'gemini-3.8-flash-low',
        upstream_model: 'gemini-3.8-flash-n',
    });
    assert.equal(count(markup, 'cm-chain-arrow'), 1);
    assert.equal(count(markup, 'cm-chain-branch'), 1);
    assert.equal(text(markup), 'gemini-2.5-flash-lite gemini-3.8-flash-low 上游 gemini-3.8-flash-n');
});

test('model that was neither rerouted nor normalised stays plain', () => {
    const markup = cell({
        request_model: 'gemini-2.5-flash',
        model_name: 'gemini-2.5-flash',
        upstream_model: 'gemini-2.5-flash',
    });
    assert.equal(text(markup), 'gemini-2.5-flash');
    assert.equal(count(markup, 'cm-chain'), 0);
});
