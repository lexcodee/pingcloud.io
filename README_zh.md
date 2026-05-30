# PingCloud.io — 云厂商 Region 时延监控平台

PingCloud.io 是一个基于开源探针网络的云厂商 Region 时延监控平台。它利用 Globalping 分布在全球 900+ 城市、100+ 国家的真实 ISP 探针节点，每日自动发起大规模网络测量——覆盖 AWS、GCP、Azure、阿里云、腾讯云、华为云、Oracle Cloud 七家主流云厂商的 150+ Region。所有测量结果经统计聚合后，生成二种维度的时延排行榜（国家/地理区域、Region），同时提供在线测试工具，允许开发者从任意探针位置发起实时 Ping/HTTP 测量，辅助应用部署云厂商Region选择决策，特别是一个应用同时覆盖多个国家时。整个项目面向开源社区设计，数据透明、架构简洁、部署轻量。

**🌐 在线访问：[pingcloud.io](https://pingcloud.io)**

---

## 核心功能

| 功能 | 说明 | 用户价值 |
|------|------|----------|
| **全球探针网络** | 基于 Globalping 开源探针平台，覆盖 900+ 城市、100+ 国家、真实 ISP 节点 | 不仅是数据中心节点，更能反馈真实终端用户网络视角 |
| **在线实时测试** | 浏览器端直接调用 Globalping API，从任意位置发起 Ping/HTTP 测量 | 无需安装工具、无需注册，即时验证全球任意位置到任意目标的网络质量 |
| **云厂商 Region 对比** | 同一时刻从多城市对同一 Region 发起测量，确保公平对比 | 避免不同时间点网络波动带来的对比偏差，实现真正 apples-to-apples 比较 |
| **二维度排行榜** | 按国家/地理区域（17个区域）最优云厂商region排行榜和按厂商region最优覆盖国家排行榜，支持 3/15/30 天多周期 | 从不同决策视角提供排名数据：按国家选 Region、按 Region 选国家、按地理区域分析 |
| **每日更新** | 后端持续运行测试周期，排行榜每日刷新 | 获取最新的网络质量数据，及时发现网络劣化或改善 |
| **多云厂商覆盖** | AWS、GCP、Azure、阿里云、腾讯云、华为云、Oracle，7 家共 150+ Region | 一站式对比多云网络质量，无需分别访问各家厂商工具 |
| **探针状态监控** | 实时同步 Globalping 探针列表，显示各城市在线探针数量 | 了解全球探针覆盖情况，判断测量数据的可信度 |
| **智能刷新策略** | 双轴排名优先级队列（HIGH/MID），热点数据高频更新 | 高效利用测试配额，确保重要数据保持新鲜 |

---

## 独特价值

### 真实 ISP 视角，而非数据中心视角

传统云厂商延迟测试工具（如 CloudPing、GCP Ping）运行在数据中心或云 VM 上，测量的是**数据中心到数据中心**的延迟——这并不代表真实用户的网络体验。PingCloud 基于 Globalping 的社区探针网络，探针部署在家庭宽带、办公网络等真实 ISP 环境中，测量结果更接近终端用户的实际体验。

### 同时刻多城市并发测量，Apples-to-Apples 对比

不同时间点的网络状态差异巨大（路由抖动、拥塞波动），分别测量的结果不可直接对比。PingCloud 对同一 Region 从多个城市**同时发起测量**，确保对比基准一致，消除时间维度的偏差。

### 二维度交叉排名，覆盖不同决策场景

- **按国家/地理区域选 Region**：我的用户主要在东南亚，应该选哪个云厂商的哪个 Region？
- **按 Region 选国家**：AWS ap-southeast-1 实际覆盖哪些国家效果最好？
- **按地理区域分析**：整个 Northern Europe 区域，哪家云厂商的综合表现最优？

### 多云一站式，无需分别访问各家工具

7 家云厂商、150+ Region 的延迟数据集中在一个平台，支持跨厂商横向对比。无需分别打开 CloudPing、Azure Speed Test、阿里云测速等独立工具。

---

## 独特技术实现

### 1. 探针锚点复用（Probe Anchor Reuse）

Globalping API 每次测量需要指定探针位置。首次测量使用 `country+city` 定位探针，获得 `measurement_id`；后续同一城市的所有 endpoint 测量直接复用该 `measurement_id` 作为探针锚点（`locations` 字段 = measurement ID 字符串）。这确保同一城市的所有测量来自同一探针节点，保证对比一致性，同时减少探针分配开销。

### 2. 双轴排名优先级队列（Dual-Axis Priority Queue）

刷新模式不是简单地重测所有数据，而是通过双轴排名构建智能优先级队列：

| 排名轴 | 分组方式 | 排名依据 |
|--------|----------|----------|
| **国家排名** | 按 (vendor, region) 分组 | 各国 test_count 加权中位数排名 |
| **区域排名** | 按 country 分组 | 各 (vendor, region) 中位数排名 |

合并规则：HIGH = 两个轴 HIGH 的并集；MID = 两个轴 MID 的并集减去 HIGH。

| 优先级 | 排名范围 | 刷新间隔 |
|--------|----------|----------|
| HIGH   | 前 8 名  | 24 小时  |
| MID    | 前 20 名 | 72 小时  |

超出前 20 名的对不自动重测，高效利用 Globalping API 配额。

### 3. 多数交集算法（Majority Intersection）

地理区域排行榜（`geo_region_ranking`）使用多数交集算法：一个 vendor/region 必须在该地理区域 ≥50% 的国家中出现，才纳入排名。关键实现：交集计算在**完整未截断**的 per-country 数据上进行，截断到 top 20 仅在最终 geo_region 层执行——避免短周期因截断偏差反而比长周期有更多条目的悖论。

### 4. test_count 加权聚合

所有统计值（median_ms, avg_ms, loss_pct）按 `test_count` 加权平均，代表所有测试样本的真实均值，而非简单平均各城市数据（会因样本量差异产生偏差）。

### 5. 代理端口池 IP 轮换

通过 Oxylabs DC 代理（`dc.oxylabs.io:8001-63000`）绕过 Globalping token 限流。`OxylabsProxyManager` 维护端口池（大小 = city_concurrency），每个并发城市任务分配独立槽位（`slot = city_index % pool_size`），不同城市使用不同 IP。429 限流时立即轮换端口重试（不计入常规重试次数），实现高吞吐采集。

### 6. 客户端直连 Globalping API

在线测试功能无需后端参与——浏览器直接调用 `https://api.globalping.io/v1/measurements`（匿名访问），结果实时流式返回（500ms 轮询 + `inProgressUpdates: true`）。前端零后端依赖，部署极简。

---

## 架构概览

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  cities.csv  │     │  endpoints.csv   │     │  Globalping API │
│   (924 行)   │     │    (152 行)      │     │  (全球探针网络)  │
└──────┬───────┘     └────────┬─────────┘     └────────┬────────┘
       │                      │                         │
       ▼                      ▼                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      PostgreSQL                               │
│  cities ──┐                                                  │
│            ├── latency_results ──→ ranking.py ──→ JSON 排行榜 │
│  endpoints ┘                                                  │
│            ├── hourly_quota (限流)                             │
│            └── task_state (任务状态)                           │
└──────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │  run_web.py      │
                            │  (静态文件服务器)  │
                            │  → 前端排行榜展示  │
                            └──────────────────┘
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install asyncpg aiohttp pyyaml
```

### 2. 准备数据库

确保 PostgreSQL 已运行，并导入基础数据：

```bash
psql -h localhost -U postgres -c "\copy cities(...) FROM cities.csv WITH (FORMAT csv, HEADER true);"
psql -h localhost -U postgres -c "\copy cloud_endpoints(...) FROM endpoints.csv WITH (FORMAT csv, HEADER true);"
```

> `latency_results`、`hourly_quota`、`task_state` 表由程序自动创建，无需手动建表。

### 3. 同步探针列表 & 填充中文城市名

```bash
python3 update_probes.py
python3 update_city_cn.py
```

### 4. 运行采集

```bash
# 首次全量测试（所有城市 × 所有 endpoint，支持断点续跑）
python3 main.py --mode init

# 增量刷新（按双轴排名优先级重测过期数据）
python3 main.py --mode refresh
```

### 5. 启动 Web 服务器

```bash
python3 run_web.py
```

---

## 排行榜输出

每次测试完成后生成三组 JSON 文件（每组对应一个聚合周期）：

| 文件 | 维度 | 说明 |
|------|------|------|
| `country_ranking_*.json` | 按 vendor/region | 时延最低的前 20 个国家 |
| `region_ranking_*.json` | 按 country | 时延最低的前 20 个 vendor/region |
| `geo_region_ranking_*.json` | 按 UN 地理区域 | 时延最低的前 20 个 vendor/region（多数交集算法） |

排行榜条目字段：`rank`, `median_ms`, `avg_ms`, `loss_pct`, `city_count`, `test_count`

默认聚合周期：3/15/30 天（可通过 `--ranking-periods` 配置）。

---

## 项目结构

```
├── main.py              # 入口：解析参数，调度模式
├── config.py            # 参数定义与加载，数据库凭据，Globalping token
├── db.py                # asyncpg 连接池与 CRUD
├── globalping.py        # Globalping API 封装（创建/轮询/解析 ping 测量）
├── proxy_manager.py     # Oxylabs DC 代理端口池管理器（多槽位 IP 轮换）
├── scheduler.py         # 并发调度与限流逻辑（init/refresh 模式）
├── ranking.py           # 排行榜 JSON 生成（多周期、三维度）
├── update_probes.py     # 同步 Globalping 探针列表到 cities 表
├── update_city_cn.py    # 填充 cities.city_cn 中文城市名
├── gen_cities_json.py   # 从 DB 生成 cities.json 供前端使用
├── run_web.py           # 独立 Web 服务器（静态文件 + gzip）
├── cities.csv           # 城市数据源（924 行）
├── endpoints.csv        # 云端点数据源（152 行）
└── web/                 # 前端 Web 应用
    └── static/
        ├── index.html   # 单页应用（SPA）
        ├── app.js       # 主 JS：在线测试 + 排行榜 + 探针网络
        ├── i18n.js      # 国际化引擎
        ├── i18n/        # en.json / zh.json 翻译文件
        ├── tailwind.css # 编译后的 Tailwind CSS
        └── data/
            ├── cities.json     # 城市列表（含 i18n 名称 + probe_num）
            ├── endpoints.json  # 云端点列表（vendor, region, hostname）
            ├── countries.json  # 国家列表（含国旗）
            └── rankings/      # 排行榜 JSON 文件
                ├── country_ranking_*.json
                ├── region_ranking_*.json
                ├── geo_region_ranking_*.json
                └── periods.json
```

---

## 配置参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--city-concurrency` | 最大并发城市数 | 15 |
| `--endpoint-concurrency` | 每城市 endpoint 并发数 | 20 |
| `--hourly-limit` | 每小时最大测试次数 | 5000 |
| `--max-cities-per-country` | 每国家最大城市数（探针加权采样） | 10 |
| `--rank-high-threshold` | 高频刷新排名阈值 | 8 |
| `--rank-mid-threshold` | 中频刷新排名阈值 | 20 |
| `--globalping-mode` | Globalping 访问方式：`direct` 或 `proxy` | `proxy` |
| `--ranking-periods` | 排行榜聚合周期（天） | 3 15 30 |
| `--web-port` | Web 服务器端口 | 80 |

参数优先级：**CLI 参数 > config.yaml > 默认值**

---

## 技术栈

| 类别 | 技术 |
|------|------|
| **运行时** | Python 3.12+ with asyncio |
| **HTTP 客户端** | aiohttp（Globalping API 调用） |
| **HTTP 服务器** | aiohttp（静态文件 + gzip） |
| **数据库** | asyncpg（PostgreSQL，无 ORM） |
| **配置** | PyYAML + argparse（CLI > config.yaml > 默认值） |
| **前端** | 原生 JS + Tailwind CSS + Material Symbols |
| **代理** | Oxylabs DC 代理（端口池 IP 轮换） |

---

## License

MIT
