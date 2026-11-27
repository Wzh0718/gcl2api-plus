/* Progressive disclosure keeps the original fields and save payload intact. */
(() => {
    const groups = [
        ['账号复检', 'antigravity403Recheck'],
        ['模型与客户端版本', 'antigravityModelFallbackChain|antigravityHub'],
        ['请求节奏', 'antigravityPacing'],
        ['全局容量与休眠', 'antigravityFleet|antigravityDormant'],
        ['账号用量与额度保护', 'antigravityAccount|antigravityQuotaGuard'],
        ['输出抗截断', 'antiTruncation']
    ];
    function refresh() {
        document.querySelectorAll('.compatibility-option').forEach(card => {
            const fields = [...card.querySelectorAll('input,select,textarea')];
            const switches = fields.filter(field => field.type === 'checkbox');
            const values = fields.filter(field => field.type !== 'checkbox' && field.value.trim());
            card.querySelector('.compatibility-option-summary').textContent = switches.length
                ? `${switches.filter(field => field.checked).length} / ${switches.length} 项已开启 · ${values.length} 项参数已配置`
                : values.length ? values.slice(0, 2).map(field => field.value).join(' · ') : '使用默认设置';
        });
    }
    window.refreshCompatibilitySummary = refresh;
    window.revealCompatibilityField = field => {
        const category = field.closest('.config-category-panel');
        if (category?.hidden) switchConfigTab(category.id.replace('config-panel-', ''));
        const panel = field.closest('.compatibility-option-fields');
        if (panel?.hidden) panel.parentElement.querySelector('button').click();
    };
    document.addEventListener('DOMContentLoaded', () => {
        const grid = document.querySelector('.compatibility-grid');
        if (!grid) return;
        groups.forEach(([title, pattern], index) => {
            const matcher = new RegExp(`^(?:${pattern})`);
            const rows = [...grid.children].filter(row => [...row.querySelectorAll('[id]')].some(field => matcher.test(field.id)));
            if (!rows.length) return;
            const card = document.createElement('section');
            card.className = 'compatibility-option';
            const heading = document.createElement('div'); heading.className = 'compatibility-option-head';
            const copy = document.createElement('div');
            const label = document.createElement('h5'); label.textContent = title;
            const summary = document.createElement('p'); summary.className = 'compatibility-option-summary';
            const button = document.createElement('button'); button.type = 'button'; button.className = 'api-key-refresh'; button.textContent = '设置';
            button.setAttribute('aria-label', `设置${title}`); button.setAttribute('aria-expanded', 'false');
            const panel = document.createElement('div'); panel.className = 'compatibility-option-fields'; panel.id = `compatibility-options-${index}`; panel.hidden = true;
            button.setAttribute('aria-controls', panel.id);
            copy.append(label, summary); heading.append(copy, button); card.append(heading, panel);
            grid.insertBefore(card, rows[0]); rows.forEach(row => panel.append(row));
            button.addEventListener('click', () => { panel.hidden = !panel.hidden; button.setAttribute('aria-expanded', String(!panel.hidden)); button.textContent = panel.hidden ? '设置' : '收起'; });
        });
        grid.addEventListener('input', refresh);
        grid.addEventListener('change', refresh);
        document.getElementById('configForm')?.addEventListener('input', () => window.setConfigSaveFeedback('dirty', '有未保存的更改'));
        refresh();
    });
    window.setConfigSaveFeedback = (state, message) => {
        const button = document.querySelector('[onclick="saveConfig()"]');
        if (!button) return;
        if (state === 'dirty' && button.getAttribute('aria-busy') === 'true') return;
        let status = document.getElementById('configSaveFeedback');
        if (!status) {
            status = document.createElement('span'); status.id = 'configSaveFeedback'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
            button.closest('.config-toolbar').append(status);
        }
        status.dataset.state = state; status.textContent = message;
        button.disabled = state === 'saving'; button.setAttribute('aria-busy', String(state === 'saving'));
        button.textContent = state === 'saving' ? '保存中…' : state === 'success' ? '✓ 已保存' : '保存更改';
    };
})();
