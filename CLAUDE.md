# PingCloud — Cloud Region Latency Collector

## Project Overview

A Python async service that measures network latency from global cities to cloud vendor regions using the Globalping probe network. Results are stored in PostgreSQL and exported as JSON ranking files for frontend consumption.

## Tech Stack

- **Runtime**: Python 3.12+ with asyncio
- **HTTP**: aiohttp (Globalping API calls)
- **Database**: asyncpg (PostgreSQL, no ORM)
- **Config**: PyYAML + argparse (CLI > config.yaml > defaults)

## Project Structure

```
main.py           # Entry point: parse args, dispatch mode
config.py         # DB credentials, Globalping token, CLI args + config.yaml
db.py             # asyncpg pool, DDL, CRUD, quota gating, probe status
globalping.py     # Globalping API wrapper (create/poll/parse ping measurements)
proxy_manager.py  # Oxylabs DC proxy port-pool manager (multi-slot IP rotation)
scheduler.py      # Concurrency orchestration for init & refresh modes
ranking.py        # JSON ranking file generation (multi-period)
update_probes.py  # Sync live probe list from Globalping API to cities table
update_city_cn.py # Populate cities.city_cn with built-in Chinese city name mapping
gen_cities_json.py# Generate web/static/data/cities.json from DB cities table
build_i18n.py     # SSG build: generates pre-rendered index.en.html / index.zh.html from template + i18n JSON
run_web.py        # Standalone aiohttp web server (static files only, no DB, supports SSL, per-language routing)
spec.yaml         # Globalping OpenAPI spec (read-only, do not modify)
cities.csv        # Source data for cities table (924 rows)
endpoints.csv     # Source data for cloud_endpoints table (152 rows)
certs/            # SSL certificate files (gitignored)
  pingcloud.io.pem  # Cloudflare Origin certificate
  pingcloud.io.key  # Cloudflare Origin private key
web/              # Frontend web app
  static/
    index.html    # Single-page app shell (i18n template — build_i18n.py generates per-language HTML)
    index.en.html # Pre-rendered English HTML (generated, SEO-crawlable)
    index.zh.html # Pre-rendered Chinese HTML (generated, SEO-crawlable)
    app.js        # Main JS: Globalping online test + leaderboard (ranking data from static JSON)
    i18n.js       # i18n engine (URL-based lang detection, toggle navigates between / and /zh/)
    i18n/         # en.json, zh.json translation files
    tailwind.css  # Compiled Tailwind CSS (custom design tokens)
    data/
      cities.json       # City/country list with i18n names + probe_num (generated from DB)
      endpoints.json     # Cloud endpoint list (vendor, region, hostname)
      rankings/         # Ranking JSON files (copied from ./rankings/)
      countries.json    # Country list with flags
```

## Database

- Host: localhost / Port: 5432 / User: postgres / DB: postgres
- Existing tables: `cities`, `cloud_endpoints` (populated from CSVs)
- Application tables: `latency_results`, `hourly_quota` (auto-created by `db.ensure_tables()`)
- `cities.probe_num`: actual probe count from Globalping (0 = no probes found / offline city)
- `cities.city_cn`: Chinese city name (populated by `update_city_cn.py`)
- `cities.region`: UN geographic region name (e.g. "Western Asia", "Northern Europe"); used by `ranking.py` to group countries for `geo_region_ranking` generation
- `latency_results` unique constraint: `(city_id, endpoint_id, test_round)` — prevents duplicate inserts from concurrent init runs

## Running

```bash
# Start web server (port 8080, static files + ranking data)
python3 run_web.py

# Build pre-rendered i18n HTML (run after changes to index.html or i18n JSON)
python3 build_i18n.py

# Start with SSL — HTTP:80 + HTTPS:443 simultaneously (Cloudflare Origin cert)
python3 run_web.py --web-port 443 --ssl-cert certs/pingcloud.io.pem --ssl-key certs/pingcloud.io.key

# Start with SSL — custom HTTP port (default 80)
python3 run_web.py --web-port 443 --http-port 8080 --ssl-cert certs/pingcloud.io.pem --ssl-key certs/pingcloud.io.key

# Sync live probe list: update probe_num and insert new probe cities
python3 update_probes.py

# Populate city_cn column with Chinese city names
python3 update_city_cn.py

# Regenerate cities.json from DB (after city data changes)
python3 gen_cities_json.py

# First-time full test (all cities x all endpoints)
python3 main.py --mode init

# Resume interrupted init (skips already-tested pairs)
python3 main.py --mode init

# Refresh based on dual-axis ranking priority (HIGH/MID queues)
python3 main.py --mode refresh

# With custom concurrency
python3 main.py --mode init --city-concurrency 5 --endpoint-concurrency 3 --hourly-limit 300

# With config file
python3 main.py --mode init --config config.yaml

# Route Globalping API calls through Oxylabs DC proxy (bypasses token-based rate limits)
python3 main.py --mode init --globalping-mode proxy

# Direct access (default)
python3 main.py --mode init --globalping-mode direct

# Custom ranking periods (default: 1, 7, 15 days)
python3 main.py --mode init --ranking-periods 1 7 15 30
```

## Key Design Decisions

- **Probe reuse**: In both init and refresh modes, first endpoint per city uses country+city location; subsequent endpoints reuse the measurement ID as a probe anchor (Globalping `locations` field = measurement ID string, not array). Anchor is per-city per-invocation — not persisted across refresh cycles.
- **Median/mdev**: Globalping API returns `stats.avg/loss` but not median/stddev — computed from `timings[].rtt` array
- **Quota gating**: Atomic upsert on `hourly_quota` table; blocks until next hour when exhausted
- **Resume support**: `init` mode queries existing `(city_id, endpoint_id)` pairs with `loss_pct < 100` and skips them; pairs that only have 100% loss results are re-tested
- **Retry**: 3 attempts with exponential backoff (2/4/8s); `NoProbesFoundError` (422 no_probes_found) is non-retryable and raised immediately; `ProxyRateLimitError` (429 via proxy) triggers port rotation and immediate retry without counting against MAX_RETRIES (capped at 100 port rotations)
- **NoProbesFoundError**: When Globalping returns 422 `no_probes_found`, the city's `probe_num` is set to 0 and all remaining endpoints for that city are skipped immediately (no retry). On next successful test, `probe_num` is set back to 1.
- **Refresh ranking**: `_build_refresh_queues()` uses 15-day aggregated data (`db.fetch_results_for_period(15)`) to build HIGH/MID queues via dual-axis ranking: (1) Country ranking: per (vendor, region), rank countries by test_count-weighted median → top 20 = HIGH, 21-50 = MID; (2) Region ranking: per country, rank (vendor, region) by median → top 20 = HIGH, 21-50 = MID. Merge: HIGH = union of both HIGH sets; MID = (union of both MID) minus HIGH. Pairs outside top 50 in both rankings are skipped (manual only). Reuses `ranking._aggregate_cities()` for weighted aggregation. Staleness check uses `db.fetch_latest_results()` for per-pair `tested_at`.
- **Probe sync**: `update_probes.py` fetches `/v1/probes` API, resets all `probe_num` to 0, then updates matched cities with actual probe counts. New probe cities not in DB are auto-inserted, reusing `country_en`/`country_cn`/`flag_icon` from existing rows of the same country; countries absent from DB use a `COUNTRY_NAMES` fallback mapping. When run standalone, also regenerates `cities.json` via `gen_cities_json.py` subprocess. Probe update is decoupled from refresh mode — run `python3 update_probes.py` separately as needed.
- **Oxylabs DC proxy**: Optional proxy mode routes all Globalping API calls through `dc.oxylabs.io` with port-based IP rotation (ports 8001–63000). `OxylabsProxyManager` maintains a **port pool** of `city_concurrency` slots, each with its own port/IP. Ports are pre-allocated evenly across the range. Each concurrent city task is assigned a slot (`slot = city_index % pool_size`), so different cities use different IPs while endpoints within the same city share the port. Per-slot rotation: advances by `pool_size` steps after 250 uses or on 429 rate limit, wrapping back to `port_min + slot` when exhausted. Proxy mode skips Bearer token (uses anonymous Globalping quota) and bypasses local `hourly_limit` checks, since rate limits are token-based not IP-based
- **API rate limiter**: `API_CONCURRENCY=5` semaphore + `API_MIN_INTERVAL=0.2s` pacing prevents 429 "too_many_probes" errors from Globalping
- **Dedup protection**: `latency_results` has unique constraint `(city_id, endpoint_id, test_round)`. `insert_result` uses `ON CONFLICT DO NOTHING` to silently skip duplicate inserts from concurrent init runs.
- **Web frontend**: Standalone `run_web.py` serves static files only (no DB dependency). Leaderboard loads ranking JSON from `web/static/data/rankings/` and city/country names from `web/static/data/cities.json`. All `/static/` assets use `Cache-Control: public, max-age=86400`; cache-busting via `?v=` query string in HTML. Ranking date discovery: (1) Read `periods.json` first — extract date from `generated_at` field, validate with one HEAD request; (2) Fallback — scan recent 8 dates via HEAD probes if `periods.json` is missing or its date has no ranking files. No `sessionStorage` caching of the date (avoids stale cache pointing to deleted files).
- **i18n SSG (SEO-critical)**: `build_i18n.py` generates pre-rendered HTML files (`index.en.html`, `index.zh.html`) from the `index.html` template + i18n JSON. All `data-i18n` text is baked into the HTML source so search engine crawlers see fully rendered content without executing JavaScript. Server routes: `/` → English, `/zh/` → Chinese, `/zh` → 301 redirect to `/zh/`. Each page includes `<link rel="alternate" hreflang="...">` tags and `<link rel="canonical">`. Language toggle navigates between URL paths (not in-page JS swap). URL is the single source of truth for language — no localStorage persistence. Client-side i18n (`i18n.js`) still runs for dynamic JS-generated content (dropdowns, ranking tables, FAQ). Run `python3 build_i18n.py` after any changes to `index.html` or i18n JSON files.
- **Leaderboard region/country dropdown**: The "Best Region by Country" tab uses a hierarchical dropdown (same pattern as online test source dropdown): Region group headers (selectable, with map icon) → Countries (indented, with flag icons). Selecting a region loads data from `geo_region_ranking` JSON; selecting a country loads from `region_ranking` JSON. Dropdown preserves selection across language changes.
- **SSL/HTTPS**: `run_web.py` supports native SSL via `--ssl-cert` + `--ssl-key` args. When SSL is enabled, the app runs on both HTTP (`--http-port`, default 80) and HTTPS (`--web-port`, e.g. 443) simultaneously using `web.AppRunner` + `web.TCPSite`. Both ports serve the same content independently (no redirect). Uses Cloudflare Origin Certificate (`certs/pingcloud.io.pem` / `certs/pingcloud.io.key`) — domain is `pingcloud.io` behind Cloudflare CDN with SSL mode **Full (Strict)**. TLS minimum version: 1.2. Cert directory is gitignored.
- **Online test (Globalping)**: Client-side Globalping API integration — no backend dependency. Calls `https://api.globalping.io/v1/measurements` directly from the browser (anonymous access, no token). Supports Ping (5 packets, ICMP) and HTTP (HTTPS) protocols. Source location uses hierarchical dropdown: Global → Region (UN geographic regions) → Country → City. Probe Limit input (default 3). Results poll every 500ms with `inProgressUpdates: true` for live streaming. Ping results show: MIN, AVG, MED, MAX, RCV, DROP, LOSS%. HTTP results show: Status Code, Total, DNS, TCP, TLS, TTFB, Download. For HTTP measurements, target is auto-stripped to hostname (API requires hostname, not URL).
- **Online test quota**: Client-side quota display synced from Globalping API's real rate limit state. Anonymous limit: 100 tests/hour; authenticated: 250 tests/hour (rolling window, enforced server-side by Globalping). Quota data flows: (1) On page load, `fetchQuotaFromLimitsAPI()` calls `GET /v1/limits` to get `{limit, remaining, reset}` and caches to `localStorage` key `gp_probe_quota` as `{limit, remaining, resetAt}`; (2) On each test, `updateQuotaFromHeaders()` reads `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers from the 202 response and updates cache; (3) On 429, `updateQuotaFrom429()` reads `Retry-After` header and sets remaining=0. Before each test, client-side check shows early warning if `remaining < probeLimit` (avoids wasted API call), but real enforcement is server-side. UI displays "剩余 X/100" (zh) / "X/100 remaining" (en) with MM:SS countdown to `resetAt` (updates every 1s). When exhausted, text turns `text-error` and Start Test button is disabled.
- **Hierarchical source dropdown**: `buildGlobalpingSourceItems()` constructs a 2-level dropdown from cities.json: (1) "Global (Random)" with total city count, (2) Region headers (selectable, clicking selects the region for random probing within that region), (3) Countries within each region. Region headers use larger font (`text-body-sm font-semibold text-primary`) to visually distinguish from country items. City dropdown: only "Random" when source is Global or Region; specific cities with probe counts when source is a specific country.
- **Flag/icon system**: `flagIcon(value)` provides unified icon rendering across all UI components (online test, probe network, leaderboard): Global → Material Symbols `public` icon; Region → Material Symbols `map` icon; Country (ISO2) → flagcdn.com image (`https://flagcdn.com/w20/${iso2}.png`). Dropdown items use `innerHTML` (not `textContent`) to render the `<img>` and `<span>` HTML returned by `flagIcon()`.
- **cities.json generation**: `gen_cities_json.py` queries DB and writes `web/static/data/cities.json` with fields: id, region, en, cn, iso2, city, city_cn. Run after any city data changes.
- **Chinese city names**: `update_city_cn.py` contains a built-in mapping of ~949 city English→Chinese names and updates the `cities.city_cn` column. Run once to populate initial data.

## Config Priority

CLI args > config.yaml > built-in defaults (see `config.py` DEFAULTS)

## Output

Ranking JSON files written to `./rankings/`, one set per configured period. These are copied to `web/static/data/rankings/` for frontend consumption:

- `country_ranking_YYYYMMDD_p{N}d.json` — per vendor/region, top 20 countries by lowest median
- `region_ranking_YYYYMMDD_p{N}d.json` — per country, top 20 regions by lowest median
- `geo_region_ranking_YYYYMMDD_p{N}d.json` — per geographic region, top 20 vendor regions (majority intersection computed on full untruncated per-country data: vendor/region must appear in ≥50% of countries within the region; avg_ms/median_ms/loss_pct averaged across countries, city_count/test_count summed. Truncation to top 20 happens only at the final geo_region level, not before the intersection, to avoid shorter periods paradoxically having more entries than longer periods)

Default periods: 1, 7, 15 days (configurable via `--ranking-periods` or `config.yaml`).

Each ranking entry contains:
- `rank`, `median_ms`, `avg_ms`, `loss_pct`, `city_count`, `test_count`
- `city_count` — number of distinct cities with test data for this entry
- `test_count` (displayed as "Samples") — total number of test sample rows for this entry in the period

Stats (median_ms, avg_ms, loss_pct) are test_count-weighted averages across all cities for the same group key, representing the true mean across all individual test samples. Region ranking groups by (country, vendor, region); country ranking groups by (vendor, region, country).

Top-level fields: `generated_at`, `period_days`, `rankings`.
