// 多行 refresh_token 输入解析回归：每行一个、JSON 数组、逐行 JSON、去重与错误提示。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../front/common.js'), 'utf8');

function load() {
    const start = source.indexOf('function parseAntigravityRefreshTokens(');
    const end = source.indexOf('async function importAntigravityByRefreshToken(');
    assert.ok(start > 0 && end > start, '找不到 parseAntigravityRefreshTokens');
    const ctx = vm.createContext({});
    vm.runInContext(source.slice(start, end), ctx);
    return ctx;
}

const ctx = load();
const join = tokens => tokens.join('|');

test('每行一个 refresh_token 全部识别，空行与 CRLF 不影响', () => {
    const tokens = ctx.parseAntigravityRefreshTokens('1//0gA\n1//0gB\r\n\n  \n1//0gC\n');
    assert.equal(join(tokens), '1//0gA|1//0gB|1//0gC');
});

test('单个 refresh_token 仍然可用', () => {
    assert.equal(join(ctx.parseAntigravityRefreshTokens('  1//0gOnly  ')), '1//0gOnly');
});

test('重复条目只保留第一次出现', () => {
    const tokens = ctx.parseAntigravityRefreshTokens('1//0gA\n1//0gB\n1//0gA\n1//0gB');
    assert.equal(join(tokens), '1//0gA|1//0gB');
});

test('凭证 JSON 数组与单个对象按 refresh_token 字段取值', () => {
    const array = ctx.parseAntigravityRefreshTokens('[{"refresh_token":"1//0gA"},{"refresh_token":"1//0gB"}]');
    assert.equal(join(array), '1//0gA|1//0gB');
    const single = ctx.parseAntigravityRefreshTokens('{"refresh_token":"1//0gC"}');
    assert.equal(join(single), '1//0gC');
});

test('逐行 JSON（NDJSON）与裸 token 可以混排', () => {
    const tokens = ctx.parseAntigravityRefreshTokens(
        '{"refresh_token":"1//0gA"}\n1//0gB\n{"refresh_token":"1//0gC"}'
    );
    assert.equal(join(tokens), '1//0gA|1//0gB|1//0gC');
});

test('单行逗号或分号分隔的多个 token 也会拆开', () => {
    assert.equal(join(ctx.parseAntigravityRefreshTokens('1//0gA,1//0gB；1//0gC')), '1//0gA|1//0gB|1//0gC');
});

test('注释行、字段名与占位示例不会当成凭证', () => {
    const tokens = ctx.parseAntigravityRefreshTokens(
        '# 第一批\nrefresh_token\n1//0gA\nxxxx\n<token>\n1//0gAbc…'
    );
    assert.equal(join(tokens), '1//0gA');
});

test('空输入与纯占位输入给出明确提示', () => {
    assert.throws(() => ctx.parseAntigravityRefreshTokens('   \n\n '), /请粘贴 refresh_token/);
    assert.throws(() => ctx.parseAntigravityRefreshTokens('xxxx\n<token>'), /未识别到 refresh_token/);
});

test('超过 100 条时报出当前条数', () => {
    const many = Array.from({length: 101}, (_, index) => `1//0g${index}`).join('\n');
    assert.throws(() => ctx.parseAntigravityRefreshTokens(many), /1 至 100 条凭证，当前 101 条/);
    const max = Array.from({length: 100}, (_, index) => `1//0g${index}`).join('\n');
    assert.equal(ctx.parseAntigravityRefreshTokens(max).length, 100);
});

test('损坏的 JSON 行报出行号而不是整体失败', () => {
    assert.throws(
        () => ctx.parseAntigravityRefreshTokens('1//0gA\n{"refresh_token":'),
        /第 2 行不是有效的 JSON/
    );
});

test('计数字段与导入按钮随输入实时更新', () => {
    const elements = {
        antigravityRefreshTokenInput: {value: ''},
        antigravityRefreshTokenCount: {textContent: '', style: {}},
        antigravityImportBtn: {textContent: ''}
    };
    const start = source.indexOf('function updateAntigravityRefreshTokenInputState(');
    const end = source.indexOf('async function importAntigravityByRefreshToken(');
    assert.ok(start > 0 && end > start, '找不到 updateAntigravityRefreshTokenInputState');
    const context = vm.createContext({
        document: {getElementById: id => elements[id] || null}
    });
    vm.runInContext(source.slice(source.indexOf('function parseAntigravityRefreshTokens('), end), context);

    context.updateAntigravityRefreshTokenInputState();
    assert.match(elements.antigravityRefreshTokenCount.textContent, /每行一个 refresh_token/);

    elements.antigravityRefreshTokenInput.value = '1//0gA\n1//0gB';
    context.updateAntigravityRefreshTokenInputState();
    assert.equal(elements.antigravityRefreshTokenCount.textContent, '已识别 2 条凭证');
    assert.equal(elements.antigravityImportBtn.textContent, '导入 2 条凭证');

    elements.antigravityRefreshTokenInput.value = '1//0gA\n{"broken"';
    context.updateAntigravityRefreshTokenInputState();
    assert.match(elements.antigravityRefreshTokenCount.textContent, /第 2 行不是有效的 JSON/);
    assert.equal(elements.antigravityImportBtn.textContent, '导入凭证');
});
