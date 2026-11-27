const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('assert/strict');

async function checkRealtime(table) {
    const tiers = table.locator('.cm-account-tier');
    await tiers.first().waitFor({state: 'attached'});
    assert.deepEqual((await table.locator('thead th').allTextContents()).slice(-2), ['账号', '详情']);
    assert.ok((await tiers.allTextContents()).includes('Pro'));
    assert.ok((await table.locator('.cm-account-label').allTextContents()).some(label => label.includes('Ultra养号完成')));
    const extraGaps = await table.locator('.cm-account-label').evaluateAll(labels => labels.map(label => {
        const cell = label.closest('td');
        return cell.getBoundingClientRect().right - label.getBoundingClientRect().right
            - parseFloat(getComputedStyle(cell).paddingRight);
    }));
    assert.ok(Math.abs(Math.min(...extraGaps)) <= 1, `Account column has extra slack: ${extraGaps}`);
}

(async () => {
    const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH});
    try {
        for (const [route, width] of [['/', 1440], ['/mobile', 390], ['/mobile', 320]]) {
            const page = await browser.newPage({viewport: {width, height: 1000}});
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            await page.goto((process.env.PREVIEW_URL || 'http://127.0.0.1:18761') + route + '?tab=call-monitor');
            for (const view of ['account', 'key', 'realtime']) {
                await page.locator(`.cm-chip[data-view=${view}]`).click();
                await page.waitForTimeout(250);
                assert.equal(await page.locator('#callMonitorContent .status.error').count(), 0);
                assert.ok(await page.locator('#callMonitorContent tbody tr').count() > 0);
// 列合并：模型、协议、推理、服务合成一列（模型名主行 + 元信息次行），不再有独立的「推理/服务」列，也不用色块。
assert.equal(await page.locator('#callMonitorContent th', {hasText:'推理/服务'}).count(),0,'service column should be gone');
assert.equal(await page.locator('#callMonitorContent th', {hasText:'遥测'}).count(),0,'telemetry column should be merged');
// 「最近状态」与「状态」合成一列「结果」：状态码与判定只写一遍。
assert.equal(await page.locator('#callMonitorContent th', {hasText:'最近状态'}).count(),0,'status code column should be merged');
assert.equal(await page.locator('#callMonitorContent th', {hasText:'状态'}).count(),0,'status column should be merged');
assert.equal(await page.locator('#callMonitorContent th', {hasText:'结果'}).count(),1,'result column missing');
assert.ok(await page.locator('#callMonitorContent td.cm-result-cell').count()>0,'result cells missing');
const cells=await page.locator('#callMonitorContent td.cm-model-cell').count();assert.ok(cells>0,'model cells missing');
assert.equal(await page.locator('#callMonitorContent td.cm-model-cell .cm-model-name').count(),cells);
assert.equal(await page.locator('#callMonitorContent .cm-service-marks, #callMonitorContent .cm-service-legend').count(),0,'color blocks should be gone');
// 唯一的色彩是模型格左侧那条 2px 色条：每格都带任务色相变量，用来区分对话/生图/嵌入。
assert.equal(await page.locator('#callMonitorContent td.cm-model-cell[style*="--cm-task-hue"]').count(),cells);
if(view==='realtime')assert.ok(await page.locator('#callMonitorContent td.cm-detail-cell').count()>0,'detail cells missing');
const text=await page.locator('#callMonitorContent').innerText();
assert.equal(text.includes(' / antigravity'),false,'service text should not render');
assert.equal(text.includes('对话 /'),false,'task text should not render');
            }
            await checkRealtime(page.locator('#callMonitorContent .cm-table'));
            assert.deepEqual(errors, []);
            console.log(width, 'all monitor views rendered; realtime account column is compact');
            await page.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exit(1); });
