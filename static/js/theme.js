// The server renders the saved palette and font. These controls preview unsaved changes.
(() => {
    const palette = document.getElementById('adminThemeSelect');
    const font = document.getElementById('adminFontSelect');
    const notice = document.getElementById('adminThemeNotice');
    if (!palette || !font || !notice) return;

    const root = document.documentElement;
    const preview = () => {
        root.dataset.theme = palette.value;
        root.dataset.bsTheme = palette.value === 'paper' ? 'light' : 'dark';
        root.dataset.font = font.value;
        notice.textContent = 'Preview only. Save Appearance to apply these changes to the whole site.';
    };
    palette.addEventListener('change', preview);
    font.addEventListener('change', preview);
})();
