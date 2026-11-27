const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('assert/strict');

async function checkRealtime(table) {
    const tiers = table.locator('.cm-account-tier');
    await tiers.first().waitFor({state: 'attached'});
    assert.deepEqual((await table.locator('thead th').allTextContents()).slice(-3), ['账号', '遥测', '请求详情']);
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
