/* Applied in the head to avoid a light flash before the panel loads. */
(() => {
    const storageKey = 'gcli-ui-theme';
    let saved;
    try { saved = localStorage.getItem(storageKey); } catch (_) {}
    const system = matchMedia('(prefers-color-scheme: dark)');
    function apply(theme) {
        document.documentElement.dataset.theme = theme;
        document.querySelectorAll('[data-theme-choice]').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
        });
    }
    apply(saved === 'light' || saved === 'dark' ? saved : (system.matches ? 'dark' : 'light'));
    document.addEventListener('DOMContentLoaded', () => {
        apply(document.documentElement.dataset.theme);
        document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => {
            saved = button.dataset.themeChoice;
            apply(saved);
            try { localStorage.setItem(storageKey, saved); } catch (_) {}
        }));
    });
    system.addEventListener('change', event => { if (!saved) apply(event.matches ? 'dark' : 'light'); });
    window.addEventListener('storage', event => {
        if (event.key !== storageKey) return;
        saved = event.newValue;
        apply(saved === 'light' || saved === 'dark' ? saved : (system.matches ? 'dark' : 'light'));
    });
})();
