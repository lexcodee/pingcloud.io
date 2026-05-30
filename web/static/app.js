/* ── PingCloud.io Frontend ────────────────────── */

// UN geographic region i18n (English → Chinese)
const REGION_NAMES_ZH = {
  'Australia and New Zealand': '澳新地区',
  'Central Asia': '中亚',
  'Eastern Asia': '东亚',
  'Eastern Europe': '东欧',
  'Latin America and the Caribbean': '拉丁美洲和加勒比',
  'Melanesia': '美拉尼西亚',
  'Micronesia': '密克罗尼西亚',
  'Northern Africa': '北非',
  'Northern America': '北美',
  'Northern Europe': '北欧',
  'South America': '南美',
  'South-eastern Asia': '东南亚',
  'Southern Asia': '南亚',
  'Southern Europe': '南欧',
  'Sub-Saharan Africa': '撒哈拉以南非洲',
  'Western Asia': '西亚',
  'Western Europe': '西欧',
};

const CONTINENT_NAMES_ZH = {
  'Africa': '非洲',
  'Asia': '亚洲',
  'Europe': '欧洲',
  'North America': '北美洲',
  'Oceania': '大洋洲',
  'South America': '南美洲',
};

function regionDisplayName(regionEn) {
  if (currentLang === 'zh') return REGION_NAMES_ZH[regionEn] || regionEn;
  return regionEn;
}

function continentDisplayName(continentEn) {
  if (currentLang === 'zh') return CONTINENT_NAMES_ZH[continentEn] || continentEn;
  return continentEn;
}

// State
let countries = [];
let citiesData = [];
let endpoints = [];
let currentLeaderboardTab = 'region';
let currentRegionPeriod = null;
let currentCountryPeriod = null;
const PAGE_SIZE = 10;
let regionPage = 1;
let countryPage = 1;

// Globalping test state
let currentGpProtocol = 'ping'; // 'ping' or 'http'
let gpTestRunning = false;
let gpPollTimer = null;
let gpCurrentTarget = '';

// ── Probe Quota (synced from Globalping API) ────
// Globalping anonymous limit: 100 tests/hour (rolling window)
// Authenticated limit: 250 tests/hour
// We sync real quota from API response headers (X-RateLimit-*) and /v1/limits endpoint
const QUOTA_STORAGE_KEY = 'gp_probe_quota';
let quotaCountdownTimer = null;
let quotaVisible = false; // only show after user completes a test

// Quota state: { limit, remaining, resetAt }
// - limit: total quota per window (from X-RateLimit-Limit)
// - remaining: remaining quota (from X-RateLimit-Remaining)
// - resetAt: timestamp when quota resets (computed from X-RateLimit-Reset seconds)
// Globalping uses a rolling window — NOT a natural-hour boundary.
// Always sync reset time from API responses; never assume a fixed 1-hour window.
let _quotaRefreshPending = false; // guard against concurrent fetchQuotaFromLimitsAPI calls

function getProbeQuota() {
  let data = null;
  try { data = JSON.parse(localStorage.getItem(QUOTA_STORAGE_KEY)); } catch(e) {}
  if (!data || typeof data.remaining !== 'number' || typeof data.limit !== 'number') {
    // No cached data — will be populated by fetchQuotaFromLimitsAPI on page load
    data = { limit: 100, remaining: 100, resetAt: 0 };
  }
  // If reset time passed, don't assume full reset (rolling window, not natural hour).
  // Trigger a background API refresh to get the real quota state.
  if (data.resetAt > 0 && Date.now() >= data.resetAt) {
    if (!_quotaRefreshPending) {
      _quotaRefreshPending = true;
      fetchQuotaFromLimitsAPI().finally(() => { _quotaRefreshPending = false; });
    }
  }
  return data;
}

function updateQuotaFromHeaders(resp) {
  // Read X-RateLimit-* headers from Globalping API response
  const limit = parseInt(resp.headers.get('X-RateLimit-Limit'));
  const remaining = parseInt(resp.headers.get('X-RateLimit-Remaining'));
  const resetSec = parseInt(resp.headers.get('X-RateLimit-Reset'));
  if (!isNaN(limit) && !isNaN(remaining) && !isNaN(resetSec)) {
    const data = {
      limit,
      remaining: Math.max(0, remaining),
      resetAt: Date.now() + resetSec * 1000,
    };
    localStorage.setItem(QUOTA_STORAGE_KEY, JSON.stringify(data));
    quotaVisible = true;
    updateQuotaDisplay();
  }
}

function updateQuotaFrom429(resp) {
  // On 429, read Retry-After and X-RateLimit-* headers first for immediate feedback
  updateQuotaFromHeaders(resp);
  // Also try Retry-After as fallback for reset time
  const retryAfter = parseInt(resp.headers.get('Retry-After'));
  if (!isNaN(retryAfter)) {
    const data = getProbeQuota();
    data.remaining = 0;
    data.resetAt = Date.now() + retryAfter * 1000;
    localStorage.setItem(QUOTA_STORAGE_KEY, JSON.stringify(data));
    updateQuotaDisplay();
  }
  // Then fetch accurate rolling-window reset time from /v1/limits API
  fetchQuotaFromLimitsAPI();
}

async function fetchQuotaFromLimitsAPI() {
  // Call GET /v1/limits to get current quota state (rolling window, not natural hour)
  try {
    const resp = await fetch('https://api.globalping.io/v1/limits');
    if (!resp.ok) return;
    const data = await resp.json();
    const create = data?.rateLimit?.measurements?.create;
    if (create && typeof create.remaining === 'number' && typeof create.reset === 'number') {
      const quota = {
        limit: create.limit || 100,
        remaining: Math.max(0, create.remaining),
        resetAt: Date.now() + create.reset * 1000,
      };
      localStorage.setItem(QUOTA_STORAGE_KEY, JSON.stringify(quota));
      // If quota is partially used, ensure the display is visible
      if (quota.remaining < quota.limit) quotaVisible = true;
      updateQuotaDisplay();
    }
  } catch(e) {
    // Silently fail — will use cached/default quota
  }
}

function formatCountdown(ms) {
  if (ms <= 0) return '00:00';
  const totalSec = Math.ceil(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function updateQuotaDisplay() {
  const data = getProbeQuota();
  const remaining = data.remaining;
  const limit = data.limit;
  const quotaText = document.getElementById('gp-quota-text');
  const quotaCountdown = document.getElementById('gp-quota-countdown');
  const quotaInfo = document.getElementById('gp-quota-info');
  if (!quotaText || !quotaCountdown || !quotaInfo) return;

  // Only show quota info after user interacts with a test AND we have real reset data
  const hasRealResetTime = data.resetAt > 0;
  const msUntilReset = hasRealResetTime ? data.resetAt - Date.now() : 0;
  if (!quotaVisible || !hasRealResetTime || msUntilReset <= 0) {
    quotaInfo.style.display = 'none';
  } else {
    quotaInfo.style.display = '';
  }

  // Quota text: "剩余 X/100" or "X/100 remaining"
  quotaText.textContent = t('onlineTest.quotaRemaining')
    .replace('{remaining}', remaining)
    .replace('{total}', limit);

  // Countdown to reset (only when we have real data from API)
  quotaCountdown.textContent = (hasRealResetTime && msUntilReset > 0) ? formatCountdown(msUntilReset) : '00:00';

  // Color: error when exhausted, normal otherwise
  if (remaining <= 0) {
    quotaInfo.className = 'flex items-center gap-2 text-label-sm text-error mt-1';
  } else {
    quotaInfo.className = 'flex items-center gap-2 text-label-sm text-on-surface-variant mt-1';
  }

  // Disable start button when exhausted
  const btn = document.getElementById('btn-start-gp');
  if (btn && !gpTestRunning) {
    btn.disabled = remaining <= 0;
    btn.style.opacity = remaining <= 0 ? '0.5' : '';
    btn.style.cursor = remaining <= 0 ? 'not-allowed' : '';
  }
}

function startQuotaCountdown() {
  if (quotaCountdownTimer) clearInterval(quotaCountdownTimer);
  updateQuotaDisplay();
  quotaCountdownTimer = setInterval(updateQuotaDisplay, 1000);
}

// ── Init ────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Initialize i18n first (loads translations, applies language)
  await initI18n();

  // Toggle icon between clear (×) and dropdown arrow (▼)
  const inputDefaults = { 'gp-source': '', 'gp-target': '', 'gp-city': '' };
  for (const id of ['gp-source', 'gp-target', 'gp-city']) {
    const input = document.getElementById(id);
    if (!input) continue;
    const updateIcon = () => {
      const hasVal = input.value.length > 0 && input.value !== inputDefaults[id];
      _updateInputIcon(id, hasVal);
    };
    input.addEventListener('input', updateIcon);
    input.addEventListener('change', updateIcon);
    updateIcon();
  }
  // Load independent data in parallel
  await Promise.all([loadEndpoints(), loadCitiesData()]);
  // Init UI that only needs cities/endpoints immediately (don't wait for rankings)
  initGlobalpingTestUI();
  initEndpointUI();
  initProbeNetworkUI();
  initFaqUI();
  initNavHighlight();
  // Load ranking data in background, then init leaderboard UI
  loadRankingData().then(() => initLeaderboardUI());

  // Re-render dynamic content on language change
  window.addEventListener('langchange', () => {
    // Re-init Globalping dropdowns
    updateQuotaDisplay();
    const gpSrcItems = buildGlobalpingSourceItems();
    _setupDropdown('gp-source', 'gp-source-dropdown', gpSrcItems);
    _loadGpCitiesForSource();
    initLeaderboardUI();
    initEndpointUI();
    initProbeNetworkUI();
    initFaqUI();
    // Re-render current ranking if a selection is active
    if (currentLeaderboardTab === 'region') renderRegionRanking();
    else renderCountryRanking();
  });
});

// ── Data Loading ────────────────────────────────
async function loadEndpoints() {
  try {
    const res = await fetch('/static/data/endpoints.json?v=' + __DATA_VERSION, { cache: 'no-cache' });
    endpoints = await res.json();
  } catch (e) { console.error('loadEndpoints', e); }
}

async function loadCitiesData() {
  try {
    const res = await fetch('/static/data/cities.json?v=' + __DATA_VERSION, { cache: 'no-cache' });
    citiesData = await res.json();
    // Extract unique countries from cities data
    const seen = new Set();
    countries = citiesData.filter(c => {
      if (seen.has(c.iso2)) return false;
      seen.add(c.iso2);
      return true;
    }).map(c => ({ iso2: c.iso2, en: c.en, cn: c.cn }));
  } catch (e) { console.error('loadCitiesData', e); }
}

// ── Online Test UI ──────────────────────────────

function _setupDropdown(inputId, dropdownId, items) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  if (!input || !dropdown) return;

  // Remove previous listeners if re-initializing
  if (input._dropdownAbort) input._dropdownAbort.abort();
  const ac = new AbortController();
  input._dropdownAbort = ac;

  function render(filter) {
    // For city dropdown, use mutable items; for others, use static items
    const currentItems = (inputId === 'gp-city') ? window.__gpCityItems : items;
    const q = (filter || '').toLowerCase();
    let matches;
    if (!q) {
      matches = currentItems;
    } else {
      // First pass: find which group headers match, and which groups have matching children
      const groupMatched = new Set();       // group values whose label/value matches the query
      const groupWithChildMatch = new Set(); // group keys that have at least one matching child
      currentItems.forEach(it => {
        if (it.type === 'group') {
          if ((it.label || '').toLowerCase().includes(q) || (it.value || '').toLowerCase().includes(q)) {
            groupMatched.add(it.value);
          }
        } else if (it.groupKey) {
          if ((it.label || '').toLowerCase().includes(q) || (it.value || '').toLowerCase().includes(q)) {
            groupWithChildMatch.add(it.groupKey);
          }
        }
      });
      // Second pass: filter items
      matches = currentItems.filter(it => {
        if (it.type === 'group') {
          // Show group header if it matches the query, or any of its children match
          const groupKey = it.value.startsWith('region:') ? it.value.slice(7) : it.value;
          return groupMatched.has(it.value) || groupWithChildMatch.has(groupKey);
        }
        if (!it.groupKey) {
          // Non-grouped item (e.g. "Global"): simple text match
          return (it.label || '').toLowerCase().includes(q) || (it.value || '').toLowerCase().includes(q);
        }
        // Child item: show if it matches, or its parent group header matches (show all children of a matched group)
        const parentValue = 'region:' + it.groupKey;
        return (it.label || '').toLowerCase().includes(q) || (it.value || '').toLowerCase().includes(q) || groupMatched.has(parentValue);
      });
    }
    dropdown.innerHTML = '';
    matches.forEach(it => {
      const div = document.createElement('div');
      if (it.type === 'group') {
        // Selectable region group header (larger font, distinct style, body font for emoji)
        div.className = 'flex items-center gap-2 px-4 py-2.5 text-body-sm font-body-md font-semibold text-primary hover:bg-primary/20 cursor-pointer transition-colors bg-white/5';
        div.innerHTML = it.label;
        div.addEventListener('mousedown', (e) => {
          e.preventDefault();
          input.value = it.display || it.value;
          input.dataset.selectedValue = it.value;
          dropdown.classList.add('hidden');
          _updateInputIcon(inputId, true);
          input.dispatchEvent(new Event('change', { bubbles: true }));
        });
      } else {
        // Selectable item (indented if inside a group) — use body font for emoji support
        div.className = it.groupKey
          ? 'flex items-center gap-2 px-4 py-2.5 pl-8 text-body-sm font-body-md text-on-surface hover:bg-primary/20 hover:text-primary cursor-pointer transition-colors'
          : 'flex items-center gap-2 px-4 py-2.5 text-body-sm font-body-md text-on-surface hover:bg-primary/20 hover:text-primary cursor-pointer transition-colors';
        div.innerHTML = it.label;
        div.addEventListener('mousedown', (e) => {
          e.preventDefault();
          input.value = it.display || it.value;
          input.dataset.selectedValue = it.value;
          dropdown.classList.add('hidden');
          _updateInputIcon(inputId, true);
          input.dispatchEvent(new Event('change', { bubbles: true }));
        });
      }
      dropdown.appendChild(div);
    });
    if (!matches.length) {
      const div = document.createElement('div');
      div.className = 'px-4 py-2.5 text-code-snippet text-on-surface-variant';
      div.textContent = t('onlineTest.noMatches');
      dropdown.appendChild(div);
    }
  }

  input.addEventListener('focus', () => {
    // For city dropdown, show all items on focus (don't filter by current value)
    const initialFilter = (inputId === 'gp-city') ? '' : input.value;
    render(initialFilter);
    dropdown.classList.remove('hidden');
  }, { signal: ac.signal });
  input.addEventListener('input', () => { delete input.dataset.selectedValue; render(input.value); dropdown.classList.remove('hidden'); }, { signal: ac.signal });
  input.addEventListener('blur', () => { dropdown.classList.add('hidden'); }, { signal: ac.signal });
  // Keyboard navigation
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const opts = dropdown.querySelectorAll('div.cursor-pointer');
      if (!opts.length) return;
      const focused = dropdown.querySelector('div.bg-primary\\/30');
      let idx = -1;
      opts.forEach((o, i) => { if (o === focused) idx = i; });
      if (focused) focused.classList.remove('bg-primary/30');
      if (e.key === 'ArrowDown') idx = (idx + 1) % opts.length;
      else idx = idx <= 0 ? opts.length - 1 : idx - 1;
      opts[idx].classList.add('bg-primary/30');
      opts[idx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      const focused = dropdown.querySelector('div.bg-primary\\/30');
      if (focused) { focused.dispatchEvent(new MouseEvent('mousedown')); e.preventDefault(); }
      else dropdown.classList.add('hidden');
    } else if (e.key === 'Escape') {
      dropdown.classList.add('hidden');
    }
  }, { signal: ac.signal });

  // Return refresh function so callers can re-render after async data loads
  return { refresh: (filter) => render(filter != null ? filter : input.value) };
}

function _toggleDropdownClear(id) {
  const input = document.getElementById(id);
  const hasValue = input.dataset.selectedValue || input.value;
  _updateInputIcon(id, !!hasValue);
}

function _updateInputIcon(id, hasVal) {
  const input = document.getElementById(id);
  const icon = document.getElementById(id + '-icon');
  if (!icon) return;
  icon.textContent = hasVal ? 'close' : 'expand_more';
  icon.classList.toggle('cursor-pointer', hasVal);
  icon.classList.toggle('hover:text-error', hasVal);
  icon.classList.toggle('pointer-events-none', !hasVal);
  icon.onclick = hasVal ? () => { input.value = ''; delete input.dataset.selectedValue; input.dispatchEvent(new Event('change', { bubbles: true })); input.focus(); } : null;
}

// ── Globalping Test Mode ────────────────────────

function setGpProtocol(p) {
  currentGpProtocol = p;
  const btnPing = document.getElementById('btn-gp-ping');
  const btnHttp = document.getElementById('btn-gp-http');
  if (p === 'ping') {
    btnPing.className = 'flex-1 py-2 rounded-md bg-primary/20 text-primary font-label-md text-label-md border border-primary/50 shadow-sm';
    btnHttp.className = 'flex-1 py-2 rounded-md text-on-surface-variant hover:text-white font-label-md text-label-md transition-colors';
  } else {
    btnHttp.className = 'flex-1 py-2 rounded-md bg-primary/20 text-primary font-label-md text-label-md border border-primary/50 shadow-sm';
    btnPing.className = 'flex-1 py-2 rounded-md text-on-surface-variant hover:text-white font-label-md text-label-md transition-colors';
  }
}

function buildGlobalpingSourceItems() {
  const items = [];
  // "Global (Random)" at the top with total city count
  const totalCities = citiesData.length;
  items.push({
    value: 'global',
    label: `${flagIcon('global')} ${t('onlineTest.globalRandom').replace(/[🌐\s]/g, '').trim() || 'Global'} [${totalCities}]`,
    display: t('onlineTest.globalRandom').replace(/[🌐\s]/g, '').trim() || 'Global'
  });

  // Group by region
  const regionMap = {};
  citiesData.forEach(c => {
    const region = c.region || 'Other';
    if (!regionMap[region]) regionMap[region] = {};
    const iso = c.iso2.toUpperCase();
    if (!regionMap[region][iso]) {
      regionMap[region][iso] = { en: c.en, cn: c.cn, iso2: c.iso2, cityCount: 0, probeCount: 0 };
    }
    regionMap[region][iso].cityCount += 1;
    regionMap[region][iso].probeCount += (c.probe_num || 0);
  });

  // Sort regions by total probe count descending
  const sortedRegions = Object.entries(regionMap).sort((a, b) => {
    const sumA = Object.values(a[1]).reduce((s, c) => s + c.probeCount, 0);
    const sumB = Object.values(b[1]).reduce((s, c) => s + c.probeCount, 0);
    return sumB - sumA;
  });

  for (const [region, regionCountries] of sortedRegions) {
    const regionCities = Object.values(regionCountries).reduce((s, c) => s + c.cityCount, 0);
    // Region group header — selectable (clicking selects the region)
    const regionLabel = regionDisplayName(region);
    items.push({
      type: 'group',
      value: `region:${region}`,
      label: `${flagIcon('region:' + region)} ${regionLabel} [${regionCities}]`,
      display: `${regionLabel} (${t('onlineTest.regionRandom')})`
    });
    // Countries within this region, sorted by probe count desc
    const sortedCountries = Object.values(regionCountries).sort((a, b) => b.probeCount - a.probeCount);
    for (const co of sortedCountries) {
      const name = currentLang === 'zh' ? (co.cn || co.en) : co.en;
      items.push({
        value: co.iso2,
        label: `${flagIcon(co.iso2)} ${name} [${co.cityCount}]`,
        display: name,
        groupKey: region
      });
    }
  }
  return items;
}

function buildGlobalpingCityItems(sourceValue) {
  const items = [{ value: 'random', label: t('onlineTest.randomOption'), display: 'Random' }];

  // Check if it's a specific country (2-letter ISO2)
  if (sourceValue && sourceValue !== 'global' && !sourceValue.startsWith('region:')) {
    const cities = citiesData.filter(c => c.iso2.toLowerCase() === sourceValue.toLowerCase());
    cities.sort((a, b) => (b.probe_num || 0) - (a.probe_num || 0));
    for (const c of cities) {
      const name = currentLang === 'zh' ? (c.city_cn || c.city) : c.city;
      items.push({ value: c.city, label: `${name} [${c.probe_num || 0}]`, display: name });
    }
  }
  return items;
}

function _loadGpCitiesForSource() {
  const sourceInput = document.getElementById('gp-source');
  const selected = sourceInput ? (sourceInput.dataset.selectedValue || 'global') : 'global';
  window.__gpCityItems = buildGlobalpingCityItems(selected);
  if (window.__gpCityDropdownCtrl) window.__gpCityDropdownCtrl.refresh('');
  // Reset city input
  const cityInput = document.getElementById('gp-city');
  if (cityInput) {
    cityInput.value = '';
    delete cityInput.dataset.selectedValue;
    _updateInputIcon('gp-city', false);
  }
}

function initGlobalpingTestUI() {
  // Source Region/Country dropdown (hierarchical)
  const srcItems = buildGlobalpingSourceItems();
  _setupDropdown('gp-source', 'gp-source-dropdown', srcItems);

  // City dropdown
  window.__gpCityItems = [{ value: 'random', label: t('onlineTest.randomOption'), display: 'Random' }];
  window.__gpCityDropdownCtrl = _setupDropdown('gp-city', 'gp-city-dropdown', window.__gpCityItems);

  // When source changes, reload cities
  const sourceInput = document.getElementById('gp-source');
  if (sourceInput && !sourceInput._gpCityListener) {
    sourceInput._gpCityListener = true;
    sourceInput.addEventListener('change', () => _loadGpCitiesForSource());
  }

  // Target endpoint dropdown (reuse endpoints data)
  const tgtItems = endpoints.map(e => ({
    value: e.endpoint,
    label: `${e.vendor} ${e.region_id} (${e.region_name})`,
  }));
  _setupDropdown('gp-target', 'gp-target-dropdown', tgtItems);

  // Restore quotaVisible from localStorage so countdown persists across page refreshes
  const savedQuota = getProbeQuota();
  if (savedQuota.resetAt > 0 && Date.now() < savedQuota.resetAt && savedQuota.remaining < savedQuota.limit) {
    quotaVisible = true;
  }

  // Start quota countdown display and fetch real quota from Globalping API
  startQuotaCountdown();
  fetchQuotaFromLimitsAPI();
}

// ── Globalping API Test Execution ───────────────

const GLOBALPING_API_URL = 'https://api.globalping.io/v1/measurements';

async function startGlobalpingTest() {
  if (gpTestRunning) return;

  const target = document.getElementById('gp-target').value.trim();
  if (!target) { alert(t('onlineTest.pleaseEnterTarget')); return; }

  // Resolve location from source dropdown
  const sourceInput = document.getElementById('gp-source');
  const sourceValue = sourceInput.dataset.selectedValue || 'global';
  const cityInput = document.getElementById('gp-city');
  const cityValue = cityInput ? (cityInput.dataset.selectedValue || 'random') : 'random';
  const probeLimit = Math.max(1, Math.min(100, parseInt(document.getElementById('gp-probe-limit').value) || 3));

  // Quota check (client-side cache; real enforcement is server-side via Globalping API)
  const quota = getProbeQuota();
  if (quota.remaining < probeLimit) {
    const countdown = formatCountdown(quota.resetAt - Date.now());
    if (quota.remaining <= 0) {
      alert(t('onlineTest.quotaExhausted').replace('{countdown}', countdown));
    } else {
      alert(t('onlineTest.quotaInsufficient')
        .replace('{needed}', probeLimit)
        .replace('{remaining}', quota.remaining)
        .replace('{countdown}', countdown));
    }
    return;
  }

  // Build locations array for Globalping API
  let locations = [];
  if (sourceValue === 'global') {
    locations = []; // worldwide — omit locations to let API pick globally
  } else if (sourceValue.startsWith('region:')) {
    const regionName = sourceValue.slice('region:'.length);
    locations = [{ region: regionName }];
  } else {
    // Specific country (ISO2)
    const loc = { country: sourceValue.toUpperCase() };
    if (cityValue && cityValue.toLowerCase() !== 'random') {
      loc.city = cityValue;
    }
    locations = [loc];
  }

  // Build measurement request
  const isPing = (currentGpProtocol === 'ping');
  // For HTTP measurements, target must be a hostname (not a URL)
  let apiTarget = target;
  if (!isPing) {
    apiTarget = target.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  }
  const body = {
    type: isPing ? 'ping' : 'http',
    target: apiTarget,
    ...(locations.length > 0 ? { locations } : {}),
    limit: probeLimit,
    inProgressUpdates: true,
    measurementOptions: isPing
      ? { packets: 5, protocol: 'ICMP' }
      : { protocol: 'HTTPS' }
  };

  gpTestRunning = true;
  gpCurrentTarget = target;

  // Update UI
  const btn = document.getElementById('btn-start-gp');
  btn.disabled = true;
  btn.innerHTML = `<span class="material-symbols-outlined animate-spin">progress_activity</span> ${t('onlineTest.testing')}`;
  document.getElementById('btn-stop-gp').style.display = '';
  document.getElementById('gp-ping-results').style.display = isPing ? '' : 'none';
  document.getElementById('gp-http-results').style.display = isPing ? 'none' : '';
  document.getElementById('gp-ping-tbody').innerHTML = '';
  document.getElementById('gp-http-tbody').innerHTML = '';
  document.getElementById('gp-progress').textContent = t('onlineTest.creatingMeasurement');

  // Test note
  document.getElementById('gp-test-note').innerHTML = (isPing
    ? t('onlineTest.globalpingPingNote')
    : t('onlineTest.globalpingHttpNote')).replace(/\n/g, '<br>');

  try {
    // POST to Globalping API (anonymous access, no token)
    const resp = await fetch(GLOBALPING_API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (resp.status === 422) {
      const errData = await resp.json();
      if (errData.error && errData.error.type === 'no_probes_found') {
        throw new Error(t('onlineTest.noProbesFound'));
      }
      throw new Error(`422: ${JSON.stringify(errData)}`);
    }
    if (resp.status === 429) {
      // Rate limited — sync quota from 429 headers
      updateQuotaFrom429(resp);
      const quota = getProbeQuota();
      const countdown = formatCountdown(quota.resetAt - Date.now());
      throw new Error(t('onlineTest.quotaExhausted').replace('{countdown}', countdown));
    }
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`API ${resp.status}: ${errText}`);
    }

    // Sync quota from 202 response headers
    updateQuotaFromHeaders(resp);

    const data = await resp.json();
    const measurementId = data.id;

    // Poll for results
    document.getElementById('gp-progress').textContent = t('onlineTest.pollingResults');
    _pollGlobalpingMeasurement(measurementId);

  } catch (e) {
    document.getElementById('gp-progress').textContent =
      t('onlineTest.measurementFailed').replace('{error}', e.message);
    finishGlobalpingTest();
  }
}

function _pollGlobalpingMeasurement(measurementId) {
  let pollCount = 0;
  const maxPolls = 120; // 60 seconds at 500ms interval

  gpPollTimer = setInterval(async () => {
    pollCount++;
    if (pollCount > maxPolls || !gpTestRunning) {
      clearInterval(gpPollTimer);
      gpPollTimer = null;
      finishGlobalpingTest();
      return;
    }

    try {
      const resp = await fetch(`${GLOBALPING_API_URL}/${measurementId}`);
      if (!resp.ok) throw new Error(`Poll API ${resp.status}`);
      const data = await resp.json();

      // Update results for each probe result
      _renderGlobalpingResults(data);

      // Check if measurement is complete
      if (data.status !== 'in-progress') {
        clearInterval(gpPollTimer);
        gpPollTimer = null;
        const results = data.results || [];
        const successCount = results.filter(r => r.result && r.result.status === 'finished').length;
        document.getElementById('gp-progress').textContent =
          t('onlineTest.probesComplete').replace('{count}', successCount);
        finishGlobalpingTest();
      }
    } catch (e) {
      // Continue polling on transient errors
      console.error('Globalping poll error:', e);
    }
  }, 500); // 500ms poll interval (API spec minimum)
}

function stopGlobalpingTest() {
  gpTestRunning = false;
  if (gpPollTimer) {
    clearInterval(gpPollTimer);
    gpPollTimer = null;
  }
  document.getElementById('gp-progress').textContent = t('onlineTest.testCancelled');
  finishGlobalpingTest();
}

function finishGlobalpingTest() {
  gpTestRunning = false;
  const btn = document.getElementById('btn-start-gp');
  btn.disabled = false;
  btn.innerHTML = `<span class="material-symbols-outlined">rocket_launch</span> ${t('onlineTest.startGlobalpingTest')}`;
  document.getElementById('btn-stop-gp').style.display = 'none';
  // Re-apply quota-based button state
  updateQuotaDisplay();
}

// ── Globalping Result Rendering ─────────────────

function _renderGlobalpingResults(data) {
  const results = data.results || [];
  const isPing = (data.type === 'ping');

  for (const item of results) {
    const probe = item.probe || {};
    const result = item.result || {};
    const resultStatus = result.status;

    // Build a unique key for this probe row
    const probeKey = `${probe.country || ''}-${probe.city || ''}-AS${probe.asn || '0'}`;

    if (isPing) {
      _renderGpPingRow(probeKey, probe, result, resultStatus);
    } else {
      _renderGpHttpRow(probeKey, probe, result, resultStatus);
    }
  }

  // Update summary row
  if (isPing) {
    _renderGpPingSummary(results);
  } else {
    _renderGpHttpSummary(results);
  }
}

function _renderGpPingRow(key, probe, result, status) {
  const tbody = document.getElementById('gp-ping-tbody');
  if (!tbody) return;
  let row = document.getElementById(`gp-ping-row-${key}`);
  if (!row) {
    row = document.createElement('tr');
    row.id = `gp-ping-row-${key}`;
    row.className = 'border-b border-white/5 hover:bg-white/5 transition-colors';
    tbody.appendChild(row);
  }

  const stats = result.stats || {};
  const timings = result.timings || [];
  const rtts = timings.map(t => t.rtt).filter(r => r != null);

  // Compute median from timings
  const sorted = [...rtts].sort((a, b) => a - b);
  const median = sorted.length ? (sorted.length % 2 ? sorted[Math.floor(sorted.length / 2)] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2) : null;

  const countryName = _countryDisplayName(probe.country);
  const cityName = probe.city || '-';
  const probeLabel = `${cityName} (AS${probe.asn || '?'})`;

  if (status === 'failed' || status === 'offline') {
    row.innerHTML = `
      <td class="px-4 py-4 text-on-surface"><span class="inline-flex items-center gap-1">${flagIcon(probe.country)}<span>${esc(countryName)}</span></span></td>
      <td class="px-4 py-4 text-on-surface-variant">${esc(cityName)}</td>
      <td class="px-4 py-4 text-on-surface-variant">${esc(probeLabel)}</td>
      <td class="px-4 py-4 text-primary/80">${esc(gpCurrentTarget)}</td>
      <td colspan="7" class="px-4 py-4 text-error">${status === 'failed' ? 'Failed' : 'Offline'}</td>`;
    return;
  }

  // in-progress or finished — show stats
  const fmtMs = (v) => v != null ? v.toFixed(1) + 'ms' : '-';
  row.innerHTML = `
    <td class="px-4 py-4 text-on-surface"><span class="inline-flex items-center gap-1">${flagIcon(probe.country)}<span>${esc(countryName)}</span></span></td>
    <td class="px-4 py-4 text-on-surface-variant">${esc(cityName)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${esc(probeLabel)}</td>
    <td class="px-4 py-4 text-primary/80">${esc(gpCurrentTarget)}</td>
    <td class="px-4 py-4 text-secondary-container">${fmtMs(stats.min)}</td>
    <td class="px-4 py-4 ${latencyClass(stats.avg)} font-bold">${fmtMs(stats.avg)}</td>
    <td class="px-4 py-4 ${latencyClass(median)} font-bold">${fmtMs(median)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(stats.max)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${stats.rcv != null ? stats.rcv : '-'}</td>
    <td class="px-4 py-4 text-on-surface-variant">${stats.drop != null ? stats.drop : '-'}</td>
    <td class="px-4 py-4 ${(stats.loss || 0) > 5 ? 'text-error' : 'text-secondary-container'}">${stats.loss != null ? stats.loss.toFixed(1) + '%' : '-'}</td>`;
}

// ── Globalping Summary Row ─────────────────

function _renderGpPingSummary(results) {
  const tbody = document.getElementById('gp-ping-tbody');
  if (!tbody) return;

  let row = document.getElementById('gp-ping-summary');
  if (!row) {
    row = document.createElement('tr');
    row.id = 'gp-ping-summary';
    row.className = 'border-t-2 border-white/10 bg-white/5 font-bold';
    tbody.appendChild(row);
  }

  // Accumulate from finished results only
  const finished = results.filter(r => r.result && r.result.status === 'finished');
  if (!finished.length) { row.innerHTML = ''; return; }

  let sumMin = 0, sumAvg = 0, sumMed = 0, sumMax = 0, sumLoss = 0;
  let sumRcv = 0, sumDrop = 0, count = 0;

  for (const item of finished) {
    const stats = item.result.stats || {};
    const timings = item.result.timings || [];
    const rtts = timings.map(t => t.rtt).filter(r => r != null);
    const sorted = [...rtts].sort((a, b) => a - b);
    const median = sorted.length ? (sorted.length % 2 ? sorted[Math.floor(sorted.length / 2)] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2) : null;

    if (stats.min != null) sumMin += stats.min;
    if (stats.avg != null) sumAvg += stats.avg;
    if (median != null) sumMed += median;
    if (stats.max != null) sumMax += stats.max;
    if (stats.rcv != null) sumRcv += stats.rcv;
    if (stats.drop != null) sumDrop += stats.drop;
    if (stats.loss != null) sumLoss += stats.loss;
    count++;
  }

  if (!count) { row.innerHTML = ''; return; }

  const fmtMs = (v) => v != null ? v.toFixed(1) + 'ms' : '-';
  const avgMin = sumMin / count, avgAvg = sumAvg / count, avgMed = sumMed / count, avgMax = sumMax / count;
  const avgLoss = sumLoss / count;

  row.innerHTML = `
    <td class="px-4 py-4 text-primary" colspan="4">${t('onlineTest.summary')}</td>
    <td class="px-4 py-4 text-secondary-container">${fmtMs(avgMin)}</td>
    <td class="px-4 py-4 ${latencyClass(avgAvg)}">${fmtMs(avgAvg)}</td>
    <td class="px-4 py-4 ${latencyClass(avgMed)}">${fmtMs(avgMed)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(avgMax)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${sumRcv}</td>
    <td class="px-4 py-4 text-on-surface-variant">${sumDrop}</td>
    <td class="px-4 py-4 ${avgLoss > 5 ? 'text-error' : 'text-secondary-container'}">${avgLoss.toFixed(1)}%</td>`;
}

function _renderGpHttpSummary(results) {
  const tbody = document.getElementById('gp-http-tbody');
  if (!tbody) return;

  let row = document.getElementById('gp-http-summary');
  if (!row) {
    row = document.createElement('tr');
    row.id = 'gp-http-summary';
    row.className = 'border-t-2 border-white/10 bg-white/5 font-bold';
    tbody.appendChild(row);
  }

  const finished = results.filter(r => r.result && r.result.status === 'finished');
  if (!finished.length) { row.innerHTML = ''; return; }

  let sumTotal = 0, sumDns = 0, sumTcp = 0, sumTls = 0, sumTtfb = 0, sumDownload = 0;
  let count = 0;

  for (const item of finished) {
    const timings = item.result.timings || {};
    if (timings.total != null) sumTotal += timings.total;
    if (timings.dns != null) sumDns += timings.dns;
    if (timings.tcp != null) sumTcp += timings.tcp;
    if (timings.tls != null) sumTls += timings.tls;
    if (timings.firstByte != null) sumTtfb += timings.firstByte;
    if (timings.download != null) sumDownload += timings.download;
    count++;
  }

  if (!count) { row.innerHTML = ''; return; }

  const fmtMs = (v) => v != null ? v.toFixed(1) + 'ms' : '-';
  const avgTotal = sumTotal / count, avgDns = sumDns / count, avgTcp = sumTcp / count;
  const avgTls = sumTls / count, avgTtfb = sumTtfb / count, avgDownload = sumDownload / count;

  row.innerHTML = `
    <td class="px-4 py-4 text-primary" colspan="5">${t('onlineTest.summary')}</td>
    <td class="px-4 py-4 ${latencyClass(avgTotal)}">${fmtMs(avgTotal)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(avgDns)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(avgTcp)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(avgTls)}</td>
    <td class="px-4 py-4 ${latencyClass(avgTtfb)}">${fmtMs(avgTtfb)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(avgDownload)}</td>`;
}

function _renderGpHttpRow(key, probe, result, status) {
  const tbody = document.getElementById('gp-http-tbody');
  if (!tbody) return;
  let row = document.getElementById(`gp-http-row-${key}`);
  if (!row) {
    row = document.createElement('tr');
    row.id = `gp-http-row-${key}`;
    row.className = 'border-b border-white/5 hover:bg-white/5 transition-colors';
    tbody.appendChild(row);
  }

  const countryName = _countryDisplayName(probe.country);
  const cityName = probe.city || '-';
  const probeLabel = `${cityName} (AS${probe.asn || '?'})`;

  if (status === 'failed' || status === 'offline') {
    row.innerHTML = `
      <td class="px-4 py-4 text-on-surface"><span class="inline-flex items-center gap-1">${flagIcon(probe.country)}<span>${esc(countryName)}</span></span></td>
      <td class="px-4 py-4 text-on-surface-variant">${esc(cityName)}</td>
      <td class="px-4 py-4 text-on-surface-variant">${esc(probeLabel)}</td>
      <td class="px-4 py-4 text-primary/80">${esc(gpCurrentTarget)}</td>
      <td colspan="7" class="px-4 py-4 text-error">${status === 'failed' ? 'Failed' : 'Offline'}</td>`;
    return;
  }

  const timings = result.timings || {};
  const fmtMs = (v) => v != null ? v.toFixed(1) + 'ms' : '-';
  const statusCode = result.statusCode;
  const statusClass = statusCode && statusCode < 400 ? 'text-secondary-container' : 'text-error';

  row.innerHTML = `
    <td class="px-4 py-4 text-on-surface"><span class="inline-flex items-center gap-1">${flagIcon(probe.country)}<span>${esc(countryName)}</span></span></td>
    <td class="px-4 py-4 text-on-surface-variant">${esc(cityName)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${esc(probeLabel)}</td>
    <td class="px-4 py-4 text-primary/80">${esc(gpCurrentTarget)}</td>
    <td class="px-4 py-4 ${statusClass} font-bold">${statusCode || '-'}</td>
    <td class="px-4 py-4 ${latencyClass(timings.total)}">${fmtMs(timings.total)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(timings.dns)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(timings.tcp)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(timings.tls)}</td>
    <td class="px-4 py-4 ${latencyClass(timings.firstByte)}">${fmtMs(timings.firstByte)}</td>
    <td class="px-4 py-4 text-on-surface-variant">${fmtMs(timings.download)}</td>`;
}


// ── Leaderboards ────────────────────────────────
let regionRankingData = {};   // { periodDays: { countryIso2: { meta, entries } } }
let countryRankingData = {};  // { periodDays: { "Vendor/Region": { meta, entries } } }
let geoRegionRankingData = {}; // { periodDays: { geoRegionName: { entries } } }
let rankingPeriods = [];      // available period days from loaded data
let rankingGeneratedAt = '';  // ISO timestamp from ranking JSON

async function loadRankingData() {
  // Step 1: Read periods.json to get the latest date and available periods
  let dateStr = null;
  let periods = null;
  try {
    const pRes = await fetch(`/static/data/rankings/periods.json?v=${__DATA_VERSION}`, { cache: 'no-cache' });
    if (pRes.ok) {
      const pJson = await pRes.json();
      periods = pJson.periods;
      if (pJson.generated_at) {
        dateStr = pJson.generated_at.slice(0, 10).replace(/-/g, '');
      }
    }
  } catch (e) { /* fallback below */ }
  if (!periods) periods = [3, 15, 30]; // fallback default

  // Step 2: Validate the date from periods.json by checking one ranking file exists
  if (dateStr) {
    const check = await fetch(`/static/data/rankings/region_ranking_${dateStr}_p${periods[0]}d.json?v=${__DATA_VERSION}`, { method: 'HEAD' })
      .then(res => res.ok)
      .catch(() => false);
    if (!check) dateStr = null;
  }

  // Step 3: Fallback — scan recent dates (today and 7 days back) via HEAD probes
  if (!dateStr) {
    const today = new Date();
    const dateCandidates = Array.from({ length: 8 }, (_, i) => {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      return d.toISOString().slice(0, 10).replace(/-/g, '');
    });
    const headResults = await Promise.all(dateCandidates.map(ds =>
      fetch(`/static/data/rankings/region_ranking_${ds}_p${periods[0]}d.json?v=${__DATA_VERSION}`, { method: 'HEAD' })
        .then(res => res.ok ? ds : null)
        .catch(() => null)
    ));
    dateStr = headResults.find(Boolean) || null;
  }
  if (!dateStr) { console.warn('No ranking data found'); rankingPeriods = []; return; }

  // Fetch all ranking files in parallel (9 requests at once vs sequential)
  const fetchTasks = periods.flatMap(p => [
    fetch(`/static/data/rankings/region_ranking_${dateStr}_p${p}d.json?v=${__DATA_VERSION}`)
      .then(res => res.ok ? res.json() : null)
      .then(json => { if (json) { regionRankingData[p] = json.rankings; if (!rankingGeneratedAt && json.generated_at) rankingGeneratedAt = json.generated_at; } })
      .catch(e => console.error('loadRegionRanking', p, e)),
    fetch(`/static/data/rankings/country_ranking_${dateStr}_p${p}d.json?v=${__DATA_VERSION}`)
      .then(res => res.ok ? res.json() : null)
      .then(json => { if (json) { countryRankingData[p] = json.rankings; if (!rankingGeneratedAt && json.generated_at) rankingGeneratedAt = json.generated_at; } })
      .catch(e => console.error('loadCountryRanking', p, e)),
    fetch(`/static/data/rankings/geo_region_ranking_${dateStr}_p${p}d.json?v=${__DATA_VERSION}`)
      .then(res => res.ok ? res.json() : null)
      .then(json => { if (json) { geoRegionRankingData[p] = json.rankings; if (!rankingGeneratedAt && json.generated_at) rankingGeneratedAt = json.generated_at; } })
      .catch(e => console.error('loadGeoRegionRanking', p, e)),
  ]);
  await Promise.all(fetchTasks);
  rankingPeriods = periods.filter(p => regionRankingData[p] || countryRankingData[p]);
}

function initLeaderboardUI() {
  // Region ranking country dropdown — hierarchical: Region headers → Countries
  // Preserve current selection across re-init (e.g. langchange)
  const prevRegionSelected = document.getElementById('lb-region-country')?.dataset.selectedValue;

  const regionCountries = rankingPeriods.reduce((set, p) => {
    Object.keys(regionRankingData[p] || {}).forEach(k => set.add(k));
    return set;
  }, new Set());

  // Build region → countries map (only countries with ranking data)
  const lbRegionMap = {};
  citiesData.forEach(c => {
    const iso2 = c.iso2.toUpperCase();
    if (!regionCountries.has(iso2)) return;
    const region = c.region || 'Other';
    if (!lbRegionMap[region]) lbRegionMap[region] = {};
    if (!lbRegionMap[region][iso2]) {
      lbRegionMap[region][iso2] = { en: c.en, cn: c.cn, iso2: c.iso2 };
    }
  });

  // Sort regions by number of ranked countries descending
  const sortedLbRegions = Object.entries(lbRegionMap).sort((a, b) =>
    Object.keys(b[1]).length - Object.keys(a[1]).length
  );

  const countryItems = [];
  for (const [region, regionCts] of sortedLbRegions) {
    const countryCount = Object.keys(regionCts).length;
    // Region group header (selectable)
    const regionLabel = regionDisplayName(region);
    countryItems.push({
      type: 'group',
      value: `region:${region}`,
      label: `${flagIcon('region:' + region)} ${regionLabel} [${countryCount}]`,
      display: regionLabel
    });
    // Countries within this region, sorted alphabetically by display name
    const sortedCts = Object.values(regionCts).sort((a, b) => {
      const na = currentLang === 'zh' ? (a.cn || a.en) : a.en;
      const nb = currentLang === 'zh' ? (b.cn || b.en) : b.en;
      return na.localeCompare(nb);
    });
    for (const co of sortedCts) {
      const name = currentLang === 'zh' ? (co.cn || co.en) : co.en;
      countryItems.push({
        value: co.iso2,
        label: `${flagIcon(co.iso2)} ${name}`,
        display: name,
        groupKey: region
      });
    }
  }

  _setupDropdown('lb-region-country', 'lb-region-country-dropdown', countryItems);
  const countryInput = document.getElementById('lb-region-country');
  countryInput.value = '';
  countryInput.placeholder = t('leaderboards.selectRegionOrCountryPlaceholder');
  delete countryInput.dataset.selectedValue;
  _toggleDropdownClear('lb-region-country');

  // Restore previous selection if available
  if (prevRegionSelected) {
    const matchItem = countryItems.find(it => it.value === prevRegionSelected);
    if (matchItem) {
      countryInput.value = matchItem.display || prevRegionSelected;
      countryInput.dataset.selectedValue = prevRegionSelected;
      _toggleDropdownClear('lb-region-country');
    }
  }

  countryInput.addEventListener('change', () => {
    _toggleDropdownClear('lb-region-country');
    regionPage = 1;
    renderRegionRanking();
  });
  countryInput.addEventListener('input', () => _toggleDropdownClear('lb-region-country'));

  // Region period tabs
  const regionDays = rankingPeriods.length ? rankingPeriods : [1, 7, 15];
  currentRegionPeriod = regionDays[0];
  document.getElementById('region-period-tabs').innerHTML = regionDays.map((d, i) =>
    `<button class="flex-1 py-1.5 text-[11px] rounded font-label-sm ${i === 0 ? 'bg-primary/20 text-primary' : 'text-on-surface-variant hover:text-white'}" onclick="setRegionPeriod(${d}, this)">${d}d</button>`
  ).join('');

  // Country ranking vendor dropdown — derive from loaded data
  const vendorSet = new Set();
  rankingPeriods.forEach(p => {
    Object.keys(countryRankingData[p] || {}).forEach(k => vendorSet.add(k.split('/')[0]));
  });
  const vendors = [...vendorSet].sort();
  const vendorItems = vendors.map(v => ({ value: v, label: v }));
  _setupDropdown('lb-country-vendor', 'lb-country-vendor-dropdown', vendorItems);
  const vendorInput = document.getElementById('lb-country-vendor');
  vendorInput.value = '';
  vendorInput.placeholder = t('leaderboards.selectVendorPlaceholder');
  delete vendorInput.dataset.selectedValue;
  _toggleDropdownClear('lb-country-vendor');
  vendorInput.addEventListener('change', () => {
    _toggleDropdownClear('lb-country-vendor');
    countryPage = 1;
    onVendorChange();
  });
  vendorInput.addEventListener('input', () => _toggleDropdownClear('lb-country-vendor'));

  // Country period tabs
  const countryDays = rankingPeriods.length ? rankingPeriods : [1, 7, 15];
  currentCountryPeriod = countryDays[0];
  document.getElementById('country-period-tabs').innerHTML = countryDays.map((d, i) =>
    `<button class="flex-1 py-1.5 text-[11px] rounded font-label-sm ${i === 0 ? 'bg-primary/20 text-primary' : 'text-on-surface-variant hover:text-white'}" onclick="setCountryPeriod(${d}, this)">${d}d</button>`
  ).join('');
}

function setLeaderboardTab(tab) {
  currentLeaderboardTab = tab;
  const tabRegion = document.getElementById('tab-region');
  const tabCountry = document.getElementById('tab-country');
  const panelRegion = document.getElementById('panel-region');
  const panelCountry = document.getElementById('panel-country');

  if (tab === 'region') {
    tabRegion.className = 'px-6 py-4 text-label-md font-label-md border-b-2 border-primary text-primary bg-primary/5';
    tabCountry.className = 'px-6 py-4 text-label-md font-label-md border-b-2 border-transparent text-on-surface-variant hover:text-on-surface hover:bg-white/5 transition-colors';
    panelRegion.style.display = '';
    panelCountry.style.display = 'none';
  } else {
    tabCountry.className = 'px-6 py-4 text-label-md font-label-md border-b-2 border-primary text-primary bg-primary/5';
    tabRegion.className = 'px-6 py-4 text-label-md font-label-md border-b-2 border-transparent text-on-surface-variant hover:text-on-surface hover:bg-white/5 transition-colors';
    panelCountry.style.display = '';
    panelRegion.style.display = 'none';
  }
}

function setRegionPeriod(days, btn) {
  currentRegionPeriod = days;
  regionPage = 1;
  btn.parentElement.querySelectorAll('button').forEach(b => { b.className = 'flex-1 py-1.5 text-[11px] rounded font-label-sm text-on-surface-variant hover:text-white'; });
  btn.className = 'flex-1 py-1.5 text-[11px] rounded font-label-sm bg-primary/20 text-primary';
  renderRegionRanking();
}

function setCountryPeriod(days, btn) {
  currentCountryPeriod = days;
  countryPage = 1;
  btn.parentElement.querySelectorAll('button').forEach(b => { b.className = 'flex-1 py-1.5 text-[11px] rounded font-label-sm text-on-surface-variant hover:text-white'; });
  btn.className = 'flex-1 py-1.5 text-[11px] rounded font-label-sm bg-primary/20 text-primary';
  renderCountryRanking();
}

function renderRegionRanking() {
  const selectedValue = document.getElementById('lb-region-country').dataset.selectedValue;
  const tbody = document.getElementById('region-ranking-tbody');
  const emptyEl = document.getElementById('region-ranking-empty');
  const pagEl = document.getElementById('region-ranking-pagination');
  if (!selectedValue) { tbody.innerHTML = ''; emptyEl.style.display = ''; pagEl.innerHTML = ''; return; }
  const days = currentRegionPeriod || (rankingPeriods[0] || 1);
  let data;
  if (selectedValue.startsWith('region:')) {
    const geoRegionName = selectedValue.slice('region:'.length);
    data = (geoRegionRankingData[days] || {})[geoRegionName];
  } else {
    data = (regionRankingData[days] || {})[selectedValue];
  }
  if (!data || !data.entries || !data.entries.length) {
    tbody.innerHTML = '';
    emptyEl.style.display = '';
    pagEl.innerHTML = '';
    return;
  }
  emptyEl.style.display = 'none';
  const total = data.entries.length;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  if (regionPage > totalPages) regionPage = totalPages;
  const start = (regionPage - 1) * PAGE_SIZE;
  const pageEntries = data.entries.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageEntries.map(r => `
    <tr class="border-b border-white/5 hover:bg-white/5 transition-colors">
      <td class="py-4 px-4 text-on-surface-variant">${r.rank}</td>
      <td class="py-4 px-4 flex items-center gap-3">
        <span class="w-6 h-6 bg-white/10 rounded-full flex items-center justify-center text-[10px] font-bold">${vendorInitial(r.vendor)}</span>
        ${esc(r.vendor)}
      </td>
      <td class="py-4 px-4">${esc(r.region_name)}</td>
      <td class="py-4 px-4 ${latencyClass(r.avg_ms)} font-bold">${r.avg_ms !== null && r.avg_ms !== undefined ? r.avg_ms.toFixed(1) + 'ms' : '-'}</td>
      <td class="py-4 px-4 ${latencyClass(r.median_ms)} font-bold">${r.median_ms !== null && r.median_ms !== undefined ? r.median_ms.toFixed(1) + 'ms' : '-'}</td>
      <td class="py-4 px-4 ${r.loss_pct > 1 ? 'text-error' : 'text-secondary-container'}">${r.loss_pct !== null && r.loss_pct !== undefined ? r.loss_pct.toFixed(1) + '%' : '-'}</td>
      <td class="py-4 px-4 text-on-surface-variant">${r.city_count != null ? r.city_count : '-'}</td>
      <td class="py-4 px-4 text-white/40">${r.test_count != null ? r.test_count : '-'}</td>
    </tr>`).join('');
  renderPagination(pagEl, total, regionPage, totalPages, n => { regionPage = n; renderRegionRanking(); });
}

function onVendorChange() {
  const vendor = document.getElementById('lb-country-vendor').dataset.selectedValue;
  // Derive regions from loaded country ranking data keys matching this vendor
  const regionSet = new Set();
  rankingPeriods.forEach(p => {
    Object.keys(countryRankingData[p] || {}).forEach(k => {
      if (k.startsWith(vendor + '/')) regionSet.add(k.slice(vendor.length + 1));
    });
  });
  const regionItems = [...regionSet].sort().map(r => ({ value: r, label: r }));
  _setupDropdown('lb-country-region', 'lb-country-region-dropdown', regionItems);
  const regionInput = document.getElementById('lb-country-region');
  regionInput.value = '';
  regionInput.placeholder = t('leaderboards.selectRegionPlaceholder');
  delete regionInput.dataset.selectedValue;
  _toggleDropdownClear('lb-country-region');
  regionInput.addEventListener('change', () => {
    _toggleDropdownClear('lb-country-region');
    countryPage = 1;
    renderCountryRanking();
  });
  regionInput.addEventListener('input', () => _toggleDropdownClear('lb-country-region'));
}

function renderCountryRanking() {
  const vendor = document.getElementById('lb-country-vendor').dataset.selectedValue;
  const region = document.getElementById('lb-country-region').dataset.selectedValue;
  const tbody = document.getElementById('country-ranking-tbody');
  const emptyEl = document.getElementById('country-ranking-empty');
  const pagEl = document.getElementById('country-ranking-pagination');
  if (!vendor || !region) { tbody.innerHTML = ''; emptyEl.style.display = ''; pagEl.innerHTML = ''; return; }
  const key = `${vendor}/${region}`;
  const days = currentCountryPeriod || (rankingPeriods[0] || 1);
  const data = (countryRankingData[days] || {})[key];
  if (!data || !data.entries || !data.entries.length) {
    tbody.innerHTML = '';
    emptyEl.style.display = '';
    pagEl.innerHTML = '';
    return;
  }
  emptyEl.style.display = 'none';
  const total = data.entries.length;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  if (countryPage > totalPages) countryPage = totalPages;
  const start = (countryPage - 1) * PAGE_SIZE;
  const pageEntries = data.entries.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageEntries.map(c => {
    const flag = c.country_iso2 ? flagIcon(c.country_iso2) : '';
    const city = citiesData.find(ct => ct.iso2.toUpperCase() === c.country_iso2.toUpperCase());
    const countryName = city ? (currentLang === 'zh' ? (city.cn || city.en) : city.en) : c.country_en;
    return `
    <tr class="border-b border-white/5 hover:bg-white/5 transition-colors">
      <td class="py-4 px-4 text-on-surface-variant">${c.rank}</td>
      <td class="py-4 px-4">${flag} ${esc(countryName)}</td>
      <td class="py-4 px-4 ${latencyClass(c.avg_ms)} font-bold">${c.avg_ms !== null && c.avg_ms !== undefined ? c.avg_ms.toFixed(1) + 'ms' : '-'}</td>
      <td class="py-4 px-4 ${latencyClass(c.median_ms)} font-bold">${c.median_ms !== null && c.median_ms !== undefined ? c.median_ms.toFixed(1) + 'ms' : '-'}</td>
      <td class="py-4 px-4 ${c.loss_pct > 1 ? 'text-error' : 'text-secondary-container'}">${c.loss_pct !== null && c.loss_pct !== undefined ? c.loss_pct.toFixed(1) + '%' : '-'}</td>
      <td class="py-4 px-4 text-on-surface-variant">${c.city_count != null ? c.city_count : '-'}</td>
      <td class="py-4 px-4 text-white/40">${c.test_count != null ? c.test_count : '-'}</td>
    </tr>`;
  }).join('');
  renderPagination(pagEl, total, countryPage, totalPages, n => { countryPage = n; renderCountryRanking(); });
}

// ── Pagination ───────────────────────────────────
function renderPagination(container, total, current, totalPages, setPage) {
  if (totalPages <= 1 && !rankingGeneratedAt) { container.innerHTML = ''; return; }
  let html = '';
  if (rankingGeneratedAt) {
    const d = new Date(rankingGeneratedAt);
    const ts = d.toLocaleString(currentLang === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
    html += `<span class="text-[11px] text-on-surface-variant">${t('leaderboards.updatedAt')}: ${ts}</span>`;
  }
  if (totalPages > 1) {
    const btnClass = (active) => active
      ? 'px-2 py-0.5 rounded text-[11px] bg-primary/20 text-primary border border-primary/50'
      : 'px-2 py-0.5 rounded text-[11px] text-on-surface-variant hover:bg-white/10 border border-white/10';
    html += `<div class="absolute right-0 flex items-center gap-1.5">`;
    html += `<button class="${btnClass(false)}" ${current === 1 ? 'disabled style="opacity:0.3;pointer-events:none"' : ''} data-page="${current - 1}"><span class="material-symbols-outlined text-[14px]">chevron_left</span></button>`;
    for (let p = 1; p <= totalPages; p++) {
      html += `<button class="${btnClass(p === current)}" data-page="${p}">${p}</button>`;
    }
    html += `<button class="${btnClass(false)}" ${current === totalPages ? 'disabled style="opacity:0.3;pointer-events:none"' : ''} data-page="${current + 1}"><span class="material-symbols-outlined text-[14px]">chevron_right</span></button>`;
    html += `</div>`;
  }
  container.innerHTML = html;
  container.querySelectorAll('button[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = parseInt(btn.dataset.page);
      if (p >= 1 && p <= totalPages) setPage(p);
    });
  });
}

// ── Export CSV ───────────────────────────────────
function exportCSV(type) {
  let headers, rows;
  if (type === 'region') {
    const selectedValue = document.getElementById('lb-region-country').dataset.selectedValue;
    const days = currentRegionPeriod || (rankingPeriods[0] || 1);
    let data;
    if (selectedValue && selectedValue.startsWith('region:')) {
      const geoRegionName = selectedValue.slice('region:'.length);
      data = (geoRegionRankingData[days] || {})[geoRegionName];
    } else {
      data = selectedValue && ((regionRankingData[days] || {})[selectedValue]);
    }
    if (!data || !data.entries || !data.entries.length) return;
    headers = ['Rank', 'Provider', 'Region Name', 'Avg Latency (ms)', 'Median Latency (ms)', 'Loss (%)', 'Cities', 'Tests'];
    rows = data.entries.map(r => [r.rank, r.vendor, r.region_name, r.avg_ms, r.median_ms, r.loss_pct, r.city_count, r.test_count]);
  } else {
    const vendor = document.getElementById('lb-country-vendor').dataset.selectedValue;
    const region = document.getElementById('lb-country-region').dataset.selectedValue;
    const key = vendor && region ? `${vendor}/${region}` : '';
    const days = currentCountryPeriod || (rankingPeriods[0] || 1);
    const data = key && ((countryRankingData[days] || {})[key]);
    if (!data || !data.entries || !data.entries.length) return;
    headers = ['Rank', 'Country', 'Avg Latency (ms)', 'Median Latency (ms)', 'Loss (%)', 'Cities', 'Tests'];
    rows = data.entries.map(c => {
      const city = citiesData.find(ct => ct.iso2.toUpperCase() === c.country_iso2.toUpperCase());
      const countryName = city ? (currentLang === 'zh' ? (city.cn || city.en) : city.en) : c.country_en;
      return [c.rank, countryName, c.avg_ms, c.median_ms, c.loss_pct, c.city_count, c.test_count];
    });
  }
  const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = (type === 'region' && selectedValue?.startsWith('region:'))
    ? `pingcloud_region_aggregate_${selectedValue.slice('region:'.length).replace(/\s+/g, '_')}_ranking.csv`
    : `pingcloud_${type}_ranking.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Endpoint List ───────────────────────────────
function initProbeNetworkUI() {
  if (!citiesData.length) return;
  const isZh = currentLang === 'zh';

  // Aggregate by continent → region → country
  const continentMap = {};
  const regionSet = new Set();
  const countrySet = new Set();
  let totalCities = 0, totalProbes = 0;

  citiesData.forEach(c => {
    const continent = c.continent || 'Other';
    const region = c.region;
    if (!continentMap[continent]) continentMap[continent] = {};
    if (!continentMap[continent][region]) continentMap[continent][region] = {};
    const key = c.iso2;
    regionSet.add(region);
    countrySet.add(key);
    if (!continentMap[continent][region][key]) {
      continentMap[continent][region][key] = { en: c.en, cn: c.cn, iso2: c.iso2, cities: 0, probes: 0, cityList: [] };
    }
    continentMap[continent][region][key].cities++;
    continentMap[continent][region][key].probes += (c.probe_num || 0);
    continentMap[continent][region][key].cityList.push({ city: c.city, city_cn: c.city_cn, probes: c.probe_num || 0 });
    totalCities++;
    totalProbes += (c.probe_num || 0);
  });

  // Stats summary
  const statsEl = document.getElementById('probe-network-stats');
  if (statsEl) {
    statsEl.innerHTML = [
      { icon: 'public', label: isZh ? '覆盖大洲' : 'Continents', value: Object.keys(continentMap).length },
      { icon: 'map', label: isZh ? '覆盖区域' : 'Regions', value: regionSet.size },
      { icon: 'flag', label: isZh ? '覆盖国家' : 'Countries', value: countrySet.size },
      { icon: 'location_city', label: isZh ? '监测城市' : 'Cities', value: totalCities },
      { icon: 'sensors', label: isZh ? '活跃探针' : 'Probes', value: totalProbes },
    ].map(s => `
      <div class="flex items-center gap-3 bg-surface-container-high/50 rounded-lg px-4 py-3 border border-white/5">
        <span class="material-symbols-outlined text-primary text-[24px]">${s.icon}</span>
        <div>
          <div class="text-headline-md font-headline-md text-white/90">${s.value.toLocaleString()}</div>
          <div class="text-label-sm font-label-sm text-on-surface-variant">${s.label}</div>
        </div>
      </div>
    `).join('');
  }

  // Continent cards
  const contentEl = document.getElementById('probe-network-content');
  if (!contentEl) return;

  const sortedContinents = Object.entries(continentMap).sort((a, b) => {
    const sumA = Object.values(a[1]).reduce((s, rg) => s + Object.values(rg).reduce((s2, co) => s2 + co.probes, 0), 0);
    const sumB = Object.values(b[1]).reduce((s, rg) => s + Object.values(rg).reduce((s2, co) => s2 + co.probes, 0), 0);
    return sumB - sumA;
  });

  contentEl.innerHTML = sortedContinents.map(([continent, regions]) => {
    const sortedRegions = Object.entries(regions).sort((a, b) => {
      const sumA = Object.values(a[1]).reduce((s, c) => s + c.probes, 0);
      const sumB = Object.values(b[1]).reduce((s, c) => s + c.probes, 0);
      return sumB - sumA;
    });
    const cRegions = sortedRegions.length;
    const cCountries = new Set(sortedRegions.flatMap(([, cs]) => Object.keys(cs))).size;
    const cCities = sortedRegions.reduce((s, [, cs]) => s + Object.values(cs).reduce((s2, c) => s2 + c.cities, 0), 0);
    const cProbes = sortedRegions.reduce((s, [, cs]) => s + Object.values(cs).reduce((s2, c) => s2 + c.probes, 0), 0);

    return `
      <div class="bg-surface-container-high/30 rounded-lg border border-white/5 overflow-hidden">
        <button class="w-full flex items-center justify-between px-5 py-4 hover:bg-white/5 transition-colors" onclick="this.parentElement.querySelector('.probe-region-body').classList.toggle('hidden'); this.querySelector('.toggle-icon').textContent = this.parentElement.querySelector('.probe-region-body').classList.contains('hidden') ? 'expand_more' : 'expand_less'">
          <div class="flex items-center gap-3">
            <span class="material-symbols-outlined text-primary text-[24px]">public</span>
            <span class="text-[22px] font-semibold text-white/90">${esc(continentDisplayName(continent))}</span>
            <span class="text-label-sm text-on-surface-variant">${cRegions} ${isZh ? '区域' : 'regions'} · ${cCountries} ${isZh ? '国家' : 'countries'} · ${cCities} ${isZh ? '城市' : 'cities'} · ${cProbes} ${isZh ? '探针' : 'probes'}</span>
          </div>
          <span class="material-symbols-outlined text-on-surface-variant toggle-icon">expand_more</span>
        </button>
        <div class="probe-region-body hidden border-t border-white/5 space-y-3 p-4">
          ${sortedRegions.map(([region, countries]) => {
            const countryList = Object.values(countries).sort((a, b) => b.probes - a.probes);
            const rCities = countryList.reduce((s, c) => s + c.cities, 0);
            const rProbes = countryList.reduce((s, c) => s + c.probes, 0);
            return `
              <div class="bg-surface-container-high/30 rounded-lg border border-white/5 overflow-hidden">
                <button class="w-full flex items-center justify-between px-4 py-3 hover:bg-white/5 transition-colors" onclick="this.parentElement.querySelector('.probe-region-countries').classList.toggle('hidden'); this.querySelector('.toggle-icon').textContent = this.parentElement.querySelector('.probe-region-countries').classList.contains('hidden') ? 'expand_more' : 'expand_less'">
                  <div class="flex items-center gap-2">
                    <span class="material-symbols-outlined text-primary-container text-[22px]">map</span>
                    <span class="text-label-lg font-label-lg text-white/90">${esc(regionDisplayName(region))}</span>
                    <span class="text-label-sm text-on-surface-variant">${countryList.length} ${isZh ? '国家' : 'countries'} · ${rCities} ${isZh ? '城市' : 'cities'} · ${rProbes} ${isZh ? '探针' : 'probes'}</span>
                  </div>
                  <span class="material-symbols-outlined text-on-surface-variant toggle-icon text-[18px]">expand_more</span>
                </button>
                <div class="probe-region-countries hidden border-t border-white/5">
                  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 p-3">
                    ${countryList.map(co => {
                      const name = isZh ? (co.cn || co.en) : co.en;
                      const sortedCities = co.cityList.sort((a, b) => b.probes - a.probes);
                      return `
                        <div class="bg-surface-container-high/50 rounded-lg px-5 py-2.5 border border-white/5">
                          <div class="flex items-center gap-3 mb-3" style="padding-left:1.5em;">
                            ${flagIcon(co.iso2)}
                            <span class="text-label-lg font-label-lg text-white/90">${esc(name)}</span>
                            <span class="text-label-sm text-white/90 ml-3">${co.cities} ${isZh ? '城市' : 'cities'} · ${co.probes} ${isZh ? '探针' : 'probes'}</span>
                          </div>
                          <table class="w-full text-label-sm text-on-surface-variant border-collapse" style="table-layout:fixed;margin-left:1.5em;">
                            <colgroup>${'<col class="w-[10%]"/>'.repeat(10)}</colgroup>
                            ${(() => {
                              const rows = [];
                              for (let i = 0; i < sortedCities.length; i += 10) rows.push(sortedCities.slice(i, i + 10));
                              return rows.map(row => `<tr>${row.map(ct => `<td class="whitespace-nowrap" style="padding:0.5em 0;">·${esc(isZh && ct.city_cn ? ct.city_cn : ct.city)}(${ct.probes})</td>`).join('')}${row.length < 10 ? '<td colspan="' + (10 - row.length) + '"></td>' : ''}</tr>`).join('');
                            })()}
                          </table>
                        </div>
                      `;
                    }).join('')}
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }).join('');
}

function initEndpointUI() {
  const VENDOR_ORDER = ['AWS', 'Azure', 'GCP', 'Alibaba', 'Tencent', 'Oracle', 'Huawei'];
  const vendors = [...new Set(endpoints.map(e => e.vendor))].sort((a, b) => {
    const ai = VENDOR_ORDER.indexOf(a), bi = VENDOR_ORDER.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
  const tabsEl = document.getElementById('endpoint-vendor-tabs');
  tabsEl.innerHTML = vendors.map((v, i) =>
    `<button class="px-4 py-2 rounded-full font-label-md text-label-md ${i === 0 ? 'bg-primary/20 text-primary border border-primary/50' : 'bg-surface-container-high text-on-surface-variant border border-white/10 hover:border-primary/50 hover:text-primary'} transition-all hover:shadow-[0_0_10px_rgba(168,232,255,0.3)]" onclick="filterEndpoints('${esc(v)}', this)">${esc(v)}</button>`
  ).join('');
  renderEndpointCards(endpoints.filter(e => e.vendor === vendors[0]));
}

function filterEndpoints(vendor, btn) {
  btn.parentElement.querySelectorAll('button').forEach(b => {
    b.className = 'px-4 py-2 rounded-full font-label-md text-label-md bg-surface-container-high text-on-surface-variant border border-white/10 hover:border-primary/50 hover:text-primary transition-all';
  });
  btn.className = 'px-4 py-2 rounded-full font-label-md text-label-md bg-primary/20 text-primary border border-primary/50 transition-all hover:shadow-[0_0_10px_rgba(168,232,255,0.3)]';
  renderEndpointCards(endpoints.filter(e => e.vendor === vendor));
}

function renderEndpointCards(list) {
  const tbody = document.getElementById('endpoint-tbody');
  tbody.innerHTML = list.map(e => `
    <tr class="bg-white/5 hover:bg-white/10 transition-colors group">
      <td class="px-4 py-4 text-on-surface">${esc(e.region_name)} <span class="text-on-surface-variant/60 text-[11px]">(${esc(e.region_id)})</span></td>
      <td class="px-4 py-4 text-on-surface-variant">${esc(e.endpoint)}</td>
      <td class="px-4 py-4 text-right">
        <button class="inline-flex items-center gap-1 text-primary hover:text-primary-fixed transition-colors" onclick="copyText('${esc(e.endpoint)}', this)">
          <span class="material-symbols-outlined text-[18px]">content_copy</span>
          <span class="text-label-sm">${t('regionList.copy')}</span>
        </button>
      </td>
    </tr>`).join('');
}

function copyText(text, btn) {
  const showFeedback = () => {
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<span class="material-symbols-outlined text-[18px]">check</span><span class="text-label-sm">✓</span>';
      setTimeout(() => { btn.innerHTML = orig; }, 1200);
    }
  };
  const fallbackCopy = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
    showFeedback();
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(showFeedback).catch(fallbackCopy);
  } else {
    fallbackCopy();
  }
}

// ── FAQ Accordion ──────────────────────────────
function initFaqUI() {
  // FAQ is now pre-rendered in HTML for SEO — no JS generation needed.
  // On pre-rendered pages, text is already correct.
  // On non-pre-rendered pages (fallback), apply i18n manually.
  const container = document.getElementById('faq-list');
  if (!container) return;
  // Only apply i18n if the FAQ items don't already have text
  // (e.g. if served from the raw template without build_i18n)
  const firstQ = container.querySelector('[data-i18n="howItWorks.faq.q1"]');
  if (firstQ && !firstQ.textContent.trim()) {
    container.querySelectorAll('[data-i18n]').forEach(el => {
      el.innerHTML = t(el.dataset.i18n);
    });
  }
}

function toggleFaq(btn) {
  const item = btn.parentElement;
  const answer = item.querySelector('.faq-answer');
  const icon = item.querySelector('.faq-icon');
  const isOpen = answer.style.maxHeight && answer.style.maxHeight !== '0px';
  if (isOpen) {
    // Closing — only writes, no layout reads needed
    answer.style.maxHeight = '0px';
    icon.style.transform = 'rotate(0deg)';
  } else {
    // Opening — read scrollHeight BEFORE any writes to avoid forced reflow
    const scrollH = answer.scrollHeight;
    // Close all other FAQ items (writes only, after the read)
    item.parentElement.querySelectorAll('.faq-item').forEach(other => {
      if (other !== item) {
        const otherAnswer = other.querySelector('.faq-answer');
        const otherIcon = other.querySelector('.faq-icon');
        if (otherAnswer.style.maxHeight && otherAnswer.style.maxHeight !== '0px') {
          otherAnswer.style.maxHeight = '0px';
          otherIcon.style.transform = 'rotate(0deg)';
        }
      }
    });
    answer.style.maxHeight = scrollH + 'px';
    icon.style.transform = 'rotate(180deg)';
  }
}

// ── Navigation Highlight ────────────────────────
function initNavHighlight() {
  const sections = document.querySelectorAll('section[id]');
  const navLinks = document.querySelectorAll('nav a[href^="#"]');

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        navLinks.forEach(a => {
          a.classList.toggle('text-primary', a.getAttribute('href') === '#' + entry.target.id);
          a.classList.toggle('text-on-surface-variant', a.getAttribute('href') !== '#' + entry.target.id);
        });
      }
    });
  }, { threshold: 0.3 });

  sections.forEach(s => observer.observe(s));
}

// ── Helpers ─────────────────────────────────────
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function latencyClass(ms) {
  if (ms === null || ms === undefined) return 'text-on-surface-variant';
  if (ms < 50) return 'text-secondary-container';
  if (ms < 150) return 'text-primary-container';
  if (ms < 300) return 'text-on-surface';
  return 'text-error';
}

function vendorInitial(vendor) {
  const map = { 'AWS': 'A', 'GCP': 'G', 'Azure': 'Z', 'Huawei': 'H', 'Alibaba': 'L', 'Tencent': 'T', 'Oracle': 'O' };
  return map[vendor] || vendor.charAt(0).toUpperCase();
}

function formatCount(n) {
  if (!n) return '-';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

// IQR outlier removal (factor 1.5)
function iqrFilter(arr) {
  if (arr.length < 4) return arr; // too few points for meaningful IQR
  const sorted = [...arr].sort((a, b) => a - b);
  const q1Idx = (sorted.length - 1) * 0.25;
  const q3Idx = (sorted.length - 1) * 0.75;
  const q1 = sorted[Math.floor(q1Idx)] + (q1Idx % 1) * (sorted[Math.ceil(q1Idx)] - sorted[Math.floor(q1Idx)]);
  const q3 = sorted[Math.floor(q3Idx)] + (q3Idx % 1) * (sorted[Math.ceil(q3Idx)] - sorted[Math.floor(q3Idx)]);
  const iqr = q3 - q1;
  const lo = q1 - 1.5 * iqr;
  const hi = q3 + 1.5 * iqr;
  return arr.filter(v => v >= lo && v <= hi);
}

function _countryDisplayName(iso2) {
  if (!iso2) return '-';
  const entry = citiesData.find(c => c.iso2.toUpperCase() === iso2.toUpperCase());
  return entry ? (currentLang === 'zh' ? (entry.cn || entry.en) : entry.en) : iso2;
}

function flagIcon(value) {
  // Returns HTML for a flag/icon based on the dropdown value:
  // - 'global' → globe icon
  // - 'region:...' → continent/region icon
  // - ISO2 country code → flagcdn.com image
  if (!value) return '';
  if (value === 'global') {
    return `<span class="material-symbols-outlined align-middle" style="font-size:20px">public</span>`;
  }
  if (value.startsWith('region:')) {
    return `<span class="material-symbols-outlined align-middle" style="font-size:20px">map</span>`;
  }
  if (value.length === 2) {
    return `<img src="https://flagcdn.com/w20/${value.toLowerCase()}.png" style="display:inline" class="w-5 h-auto rounded-sm align-middle" alt="${value}"/>`;
  }
  return '';
}