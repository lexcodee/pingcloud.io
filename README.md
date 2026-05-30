# PingCloud.io — Cloud Region Latency Monitoring Platform
[English](README.md) | [简体中文](README_zh.md)

PingCloud.io is a cloud vendor region latency monitoring platform built on an open-source probe network. It leverages the Globalping probe network spanning 900+ cities across 100+ countries with real ISP nodes, automatically launching large-scale network measurements daily — covering 150+ regions across seven major cloud vendors: AWS, GCP, Azure, Alibaba Cloud, Tencent Cloud, Huawei Cloud, and Oracle Cloud. All measurement results are statistically aggregated to generate two-dimensional latency leaderboards (by country/geo-region and by region), while also providing an online testing tool that allows developers to initiate real-time Ping/HTTP measurements from any probe location, assisting cloud region selection decisions — especially when an application spans multiple countries. The project is designed for the open-source community with transparent data, clean architecture, and lightweight deployment.

**🌐 Live at: [pingcloud.io](https://pingcloud.io)**

---

## Core Features

| Feature | Description | User Value |
|---------|-------------|------------|
| **Global Probe Network** | Built on the Globalping open-source probe platform, covering 900+ cities, 100+ countries with real ISP nodes | Not just data center nodes — reflects real end-user network perspectives |
| **Online Real-time Testing** | Browser calls Globalping API directly, initiating Ping/HTTP measurements from any location | No tools to install, no registration required — instantly verify network quality from anywhere to any target |
| **Cloud Region Comparison** | Simultaneous multi-city measurements against the same Region ensure fair comparison | Avoids comparison bias from network fluctuations at different time points — true apples-to-apples comparison |
| **Two-dimensional Leaderboard** | Best cloud region leaderboard by country/geo-region (17 regions) and best country coverage leaderboard by vendor region, with 3/15/30-day multi-period support | Ranking data from different decision perspectives: choose Region by country, choose country by Region, analyze by geo-region |
| **Daily Updates** | Backend continuously runs measurement cycles, leaderboards refresh daily | Get the latest network quality data, promptly detect degradation or improvement |
| **Multi-cloud Coverage** | AWS, GCP, Azure, Alibaba Cloud, Tencent Cloud, Huawei Cloud, Oracle — 7 vendors, 150+ Regions | One-stop multi-cloud network quality comparison, no need to visit each vendor's tools separately |
| **Probe Status Monitoring** | Real-time sync of Globalping probe list, displaying online probe counts per city | Understand global probe coverage, judge measurement data credibility |
| **Smart Refresh Strategy** | Dual-axis ranking priority queue (HIGH/MID), high-frequency updates for hot data | Efficient use of test quota, ensuring important data stays fresh |

---

## Unique Value

### Real ISP Perspective, Not Data Center Perspective

Traditional cloud latency testing tools (like CloudPing, GCP Ping) run on data centers or cloud VMs, measuring **data-center-to-data-center** latency — which does not represent real user network experience. PingCloud leverages Globalping's community probe network where probes are deployed in home broadband, office networks, and other real ISP environments, producing measurements much closer to actual end-user experience.

### Simultaneous Multi-city Concurrent Measurements — Apples-to-Apples Comparison

Network conditions vary dramatically across time (route flapping, congestion bursts), making separately-taken measurements incomparable. PingCloud launches measurements from multiple cities **simultaneously** against the same Region, ensuring a consistent comparison baseline and eliminating time-dimension bias.

### Two-dimensional Cross-ranking for Different Decision Scenarios

- **Choose Region by country/geo-region**: My users are mainly in Southeast Asia — which vendor's which Region should I pick?
- **Choose country by Region**: Which countries does AWS ap-southeast-1 actually cover best?
- **Analyze by geo-region**: Across all of Northern Europe, which vendor performs best overall?

### Multi-cloud One-stop, No Need to Visit Each Vendor's Tools

Latency data for 7 cloud vendors and 150+ Regions is consolidated on one platform with cross-vendor horizontal comparison. No need to separately open CloudPing, Azure Speed Test, Alibaba Cloud Speed Test, and other independent tools.

---

## Unique Technical Implementation

### 1. Probe Anchor Reuse

The Globalping API requires specifying a probe location for each measurement. The first measurement uses `country+city` to locate a probe and obtains a `measurement_id`; all subsequent endpoint measurements for the same city reuse this `measurement_id` as a probe anchor (`locations` field = measurement ID string). This ensures all measurements from the same city come from the same probe node, guaranteeing comparison consistency while reducing probe allocation overhead.

### 2. Dual-Axis Priority Queue

Refresh mode doesn't simply retest all data — it builds a smart priority queue via dual-axis ranking:

| Ranking Axis | Grouping | Ranking Criterion |
|--------------|----------|-------------------|
| **Country ranking** | Group by (vendor, region) | test_count-weighted median ranking per country |
| **Region ranking** | Group by country | median ranking per (vendor, region) |

Merge rule: HIGH = union of both axes' HIGH; MID = union of both axes' MID minus HIGH.

| Priority | Ranking Range | Refresh Interval |
|----------|---------------|------------------|
| HIGH     | Top 8         | 24 hours         |
| MID      | Top 20        | 72 hours         |

Pairs outside the top 20 are not auto-retested, efficiently utilizing Globalping API quota.

### 3. Majority Intersection Algorithm

The geo-region leaderboard (`geo_region_ranking`) uses a majority intersection algorithm: a vendor/region must appear in ≥50% of countries within a geographic region to be included in the ranking. Key implementation: intersection is computed on **full untruncated** per-country data, with truncation to top 20 applied only at the final geo-region level — avoiding the paradox where shorter periods would have more entries than longer periods due to truncation bias.

### 4. test_count-weighted Aggregation

All statistics (median_ms, avg_ms, loss_pct) are weighted-averaged by `test_count`, representing the true mean across all test samples rather than simply averaging city-level data (which would introduce bias from sample size differences).

### 5. Proxy Port-pool IP Rotation

Routes Globalping API calls through Oxylabs DC proxy (`dc.oxylabs.io:8001-63000`) to bypass token-based rate limits. `OxylabsProxyManager` maintains a port pool (size = city_concurrency), with each concurrent city task assigned an independent slot (`slot = city_index % pool_size`), so different cities use different IPs. On 429 rate limit, immediately rotates port and retries (not counted against regular retries), enabling high-throughput data collection.

### 6. Client-side Direct Globalping API

The online testing feature requires zero backend involvement — the browser calls `https://api.globalping.io/v1/measurements` directly (anonymous access), with results streamed back in real-time (500ms polling + `inProgressUpdates: true`). Zero backend dependency for the frontend, minimal deployment.

---

## Architecture Overview

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  cities.csv  │     │  endpoints.csv   │     │  Globalping API │
│   (924 rows) │     │   (152 rows)     │     │ (Global Probes) │
└──────┬───────┘     └────────┬─────────┘     └────────┬────────┘
       │                      │                         │
       ▼                      ▼                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      PostgreSQL                               │
│  cities ──┐                                                  │
│            ├── latency_results ──→ ranking.py ──→ JSON files │
│  endpoints ┘                                                  │
│            ├── hourly_quota (rate limiting)                    │
│            └── task_state (task metadata)                     │
└──────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │  run_web.py      │
                            │ (Static file     │
                            │  server)         │
                            │  → Leaderboard   │
                            └──────────────────┘
```

---

## Quick Start

### 1. Install Dependencies

```bash
pip install asyncpg aiohttp pyyaml
```

### 2. Prepare Database

Ensure PostgreSQL is running and import base data:

```bash
psql -h localhost -U postgres -c "\copy cities(...) FROM cities.csv WITH (FORMAT csv, HEADER true);"
psql -h localhost -U postgres -c "\copy cloud_endpoints(...) FROM endpoints.csv WITH (FORMAT csv, HEADER true);"
```

> `latency_results`, `hourly_quota`, `task_state` tables are auto-created by the program.

### 3. Sync Probe List & Populate Chinese City Names

```bash
python3 update_probes.py
python3 update_city_cn.py
```

### 4. Run Data Collection

```bash
# First-time full test (all cities × all endpoints, supports resume)
python3 main.py --mode init

# Incremental refresh (retest stale data by dual-axis ranking priority)
python3 main.py --mode refresh
```

### 5. Start Web Server

```bash
python3 run_web.py
```

---

## Leaderboard Output

Three sets of JSON files are generated after each test cycle (one per aggregation period):

| File | Dimension | Description |
|------|-----------|-------------|
| `country_ranking_*.json` | By vendor/region | Top 20 countries with lowest latency |
| `region_ranking_*.json` | By country | Top 20 vendor/regions with lowest latency |
| `geo_region_ranking_*.json` | By UN geo-region | Top 20 vendor/regions with lowest latency (majority intersection algorithm) |

Leaderboard entry fields: `rank`, `median_ms`, `avg_ms`, `loss_pct`, `city_count`, `test_count`

Default aggregation periods: 3/15/30 days (configurable via `--ranking-periods`).

---

## Project Structure

```
├── main.py              # Entry point: parse args, dispatch mode
├── config.py            # Config definition & loading, DB credentials, Globalping token
├── db.py                # asyncpg connection pool & CRUD
├── globalping.py        # Globalping API wrapper (create/poll/parse ping measurements)
├── proxy_manager.py     # Oxylabs DC proxy port-pool manager (multi-slot IP rotation)
├── scheduler.py         # Concurrency orchestration & rate limiting (init/refresh modes)
├── ranking.py           # Leaderboard JSON generation (multi-period, three dimensions)
├── update_probes.py     # Sync Globalping probe list to cities table
├── update_city_cn.py    # Populate cities.city_cn Chinese city names
├── gen_cities_json.py   # Generate cities.json from DB for frontend
├── run_web.py           # Standalone web server (static files + gzip)
├── cities.csv           # City data source (924 rows)
├── endpoints.csv        # Cloud endpoint data source (152 rows)
└── web/                 # Frontend web app
    └── static/
        ├── index.html   # Single-page app (SPA)
        ├── app.js       # Main JS: online test + leaderboard + probe network
        ├── i18n.js      # i18n engine
        ├── i18n/        # en.json / zh.json translation files
        ├── tailwind.css # Compiled Tailwind CSS
        └── data/
            ├── cities.json     # City list (with i18n names + probe_num)
            ├── endpoints.json  # Cloud endpoint list (vendor, region, hostname)
            ├── countries.json  # Country list (with flags)
            └── rankings/      # Leaderboard JSON files
                ├── country_ranking_*.json
                ├── region_ranking_*.json
                ├── geo_region_ranking_*.json
                └── periods.json
```

---

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--city-concurrency` | Max concurrent cities | 15 |
| `--endpoint-concurrency` | Concurrent endpoints per city | 20 |
| `--hourly-limit` | Max tests per hour | 5000 |
| `--max-cities-per-country` | Max cities per country (probe-weighted sampling) | 10 |
| `--rank-high-threshold` | High-frequency refresh ranking threshold | 8 |
| `--rank-mid-threshold` | Medium-frequency refresh ranking threshold | 20 |
| `--globalping-mode` | Globalping access mode: `direct` or `proxy` | `proxy` |
| `--ranking-periods` | Leaderboard aggregation periods (days) | 3 15 30 |
| `--web-port` | Web server port | 80 |

Priority: **CLI args > config.yaml > defaults**

---

## Tech Stack

| Category | Technology |
|----------|------------|
| **Runtime** | Python 3.12+ with asyncio |
| **HTTP Client** | aiohttp (Globalping API calls) |
| **HTTP Server** | aiohttp (static files + gzip) |
| **Database** | asyncpg (PostgreSQL, no ORM) |
| **Config** | PyYAML + argparse (CLI > config.yaml > defaults) |
| **Frontend** | Vanilla JS + Tailwind CSS + Material Symbols |
| **Proxy** | Oxylabs DC proxy (port-pool IP rotation) |

---

## License

MIT
