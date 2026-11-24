(() => {
  const key = 'steam-review-theme';
  const root = document.documentElement;
  const themeColor = document.querySelector('meta[name="theme-color"]');
  let toggle;

  function applyTheme(value) {
    const theme = value === 'dark' ? 'dark' : 'light';
    const changed = root.dataset.theme !== theme;
    root.dataset.theme = theme;
    themeColor.content = theme === 'dark' ? '#222b35' : '#e8eff9';
    toggle?.setAttribute('aria-checked', String(theme === 'dark'));
    if (changed) window.dispatchEvent(new Event('steam-theme-change'));
  }

  function readTheme() {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  applyTheme(readTheme());

  document.addEventListener('DOMContentLoaded', () => {
    toggle = document.getElementById('theme-switch');
    applyTheme(root.dataset.theme);
    toggle.addEventListener('click', () => {
      const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem(key, theme);
      } catch {
        // The switch also works when browser storage is unavailable.
      }
      applyTheme(theme);
    });
  }, { once: true });

  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) applyTheme(readTheme());
  });
})();
