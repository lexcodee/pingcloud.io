/* ── PingCloud i18n Engine ────────────────────── */

const translations = { en: {}, zh: {} };

// Detect language from URL path (/zh/ = Chinese, everything else = English)
// URL is the single source of truth — no localStorage persistence
function _detectLangFromURL() {
  return window.location.pathname.startsWith('/zh') ? 'zh' : 'en';
}

let currentLang = _detectLangFromURL();

// Pre-rendered pages already have correct text baked into HTML.
// Skip static text replacement on first load to avoid redundant DOM writes
// and potential flash. Only needed when switching language at runtime
// (which now navigates to a different URL, so this flag stays true).
let _isPreRendered = true;

async function initI18n() {
  try {
    const v = window.__DATA_VERSION || Date.now();
    const [en, zh] = await Promise.all([
      fetch(`/static/i18n/en.json?v=${v}`).then(r => r.json()),
      fetch(`/static/i18n/zh.json?v=${v}`).then(r => r.json()),
    ]);
    translations.en = en;
    translations.zh = zh;
  } catch (e) {
    console.error('i18n load failed', e);
  }
  applyLang(currentLang);
}

function t(key) {
  const val = translations[currentLang]?.[key];
  if (val) return val;
  const fallback = currentLang === 'en' ? 'zh' : 'en';
  return translations[fallback]?.[key] || key;
}

function applyLang(lang) {
  currentLang = lang;
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';

  // On pre-rendered pages, static text is already correct in HTML source.
  // Skip the DOM rewrite to avoid redundant manipulation and flash.
  // Dynamic JS content (dropdowns, tables, FAQ) uses t() directly
  // and is refreshed via the 'langchange' event below.
  if (!_isPreRendered) {
    // Update all [data-i18n] text content
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.dataset.i18n;
      const val = t(key);
      if (el.tagName === 'INPUT' || el.tagName === 'SELECT') {
        el.value = val;
      } else {
        el.innerHTML = val;
      }
    });

    // Update placeholders
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
  }

  // These are always needed even on pre-rendered pages:
  // - title and meta may be read by browser before JS
  // - lang toggle label is small and safe to update
  // - langchange event drives dynamic content re-render
  const titleEl = document.querySelector('title[data-i18n]');
  if (titleEl) document.title = t(titleEl.dataset.i18n);

  const metaDesc = document.querySelector('meta[name="description"][data-i18n-content]');
  if (metaDesc) metaDesc.content = t(metaDesc.dataset.i18nContent);

  const langLabel = document.getElementById('lang-label');
  if (langLabel) langLabel.textContent = t('nav.langLabel');

  // Dispatch event for app.js to re-render dynamic content
  window.dispatchEvent(new Event('langchange'));
}

function toggleLang() {
  // Navigate to the other language's URL path
  // / → /zh/ and /zh/ → /
  if (currentLang === 'zh') {
    window.location.href = '/';
  } else {
    window.location.href = '/zh/';
  }
}
