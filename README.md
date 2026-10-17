# cf-proxyip-us

[![ProxyIP Auto Update](https://github.com/LeilaoMi/cf-proxyip-us/actions/workflows/proxyip-auto-update.yml/badge.svg)](https://github.com/LeilaoMi/cf-proxyip-us/actions/workflows/proxyip-auto-update.yml)

自动维护一个**美国出口**的 Cloudflare ProxyIP：GitHub Actions 定时收集第三方 ProxyIP 候选、验证、测速排序，选出 **1 个稳定主 IP** 写进 DNS-only 域名 `proxyip.leilaomi.cc.cd`，同时用 Worker + KV 在 `list.leilaomi.cc.cd` 分发备用池和完整报告。

> 本文只写机制和默认值，不写死当前 IP、候选数量或延迟。实时数据看 `/health`、`docs/` 或 Actions 日志。

**目录**：[快速使用](#快速使用) · [接口](#接口) · [认证](#认证) · [工作原理](#工作原理) · [候选来源](#候选来源) · [候选预算](#候选预算) · [验证](#验证) · [排序](#排序与测速四个阶段) · [主-ip-与-failover](#主-ip-与-failover) · [自动化与部署](#自动化与部署) · [配置](#配置) · [本地运行](#本地运行) · [已知限制](#已知限制) · [排障](#排障指南) · [仓库结构](#仓库结构)

## 快速使用

在 edgetunnel / VLESS Worker 等客户端的 ProxyIP 字段填写：

```text
proxyip.leilaomi.cc.cd
```

| 域名 | 作用 |
|---|---|
| `proxyip.leilaomi.cc.cd` | **实际使用的 ProxyIP**。DNS-only（灰云）单 A 记录，永远只指向 1 个主 IP，TTL 300 |
| `list.leilaomi.cc.cd` | Worker 数据分发：首页、备用池、报告、Token。**不要**填进 ProxyIP 字段 |

注意：ProxyIP 只在目标网站托管在 Cloudflare 上时才会被用到；访问非 CF 站点时流量直接从你的 Worker 出口发出，与本项目无关。

## 接口

除 `/`、`/health`、`/robots.txt` 外都需要[认证](#认证)。

| 路径 | 内容 |
|---|---|
| `/` | 状态页，同时签发访问 Cookie |
| `/health` | 公开的最小健康信息：`ok`、`stale`、`age_minutes`、`count`、`checked_at` |
| `/health/full` | 详细健康信息 |
| `/current.txt` | 当前主 IP（与 DNS 一致） |
| `/current.json` | 主 IP 详情 + failover 状态 |
| `/standby.txt` | 备用池，最多 10 个，同一 ASN 最多 2 个 |
| `/top5.txt` | 主 IP + 前 4 个备用，同一 ASN 最多 1 个 |
| `/best.txt` | 排名前 20 |
| `/all.txt`、`/us.txt` | 全部通过检测的 US 候选（两者相同） |
| `/v2ray.txt`、`/base64.txt` | `all.txt` 的 Base64 编码，**不是**节点订阅 |
| `/full.json` | 完整报告（来源统计、每个 IP 的质量与测速） |
| `/state.json`、`/history.json` | failover 状态、主 IP 切换历史 |
| `/stats` | ASN / colo / 延迟分布 |
| `/token` | 当天的 HMAC Token（需 Cookie） |

数据新鲜度：`/health` 在数据超过 6 小时时 `stale=true`，超过 12 小时 `ok=false`（HTTP 状态码仍是 200）。

## 认证

两种方式，任一通过即可：

1. **签名 Cookie**：浏览器访问首页后获得 `proxyip_access=<过期时间>.<HMAC>`，有效期 8 小时。Worker 精确解析 Cookie 名并校验签名和过期时间，手写的 Cookie 无效。
2. **HMAC Token**：`YYYYMMDD-<HMAC-SHA256(secret, YYYYMMDD) hex>`，按 UTC 日期生成；当天和前一天的 Token 都有效（UTC 零点有 24 小时宽限期），使用常量时间比较。

```bash
curl -c c.txt -A Mozilla/5.0 https://list.leilaomi.cc.cd/                     # 拿 Cookie
curl -b c.txt -A Mozilla/5.0 https://list.leilaomi.cc.cd/token                # 拿 Token
curl -H "Authorization: Bearer <TOKEN>" https://list.leilaomi.cc.cd/all.txt   # 程序化访问
```

- 常见 bot、curl、wget、python-requests 等 UA 在没有合法 Token 时直接 403（Cookie 也不行），所以脚本请用 Bearer Token。
- Token 也可以用 `?t=<TOKEN>` 传，但不推荐。
- Worker Secret `PROXYIP_SECRET` 未配置时不签发 Cookie，只能使用 `ALLOW_LEGACY_DATE_TOKEN=1` 的旧日期 Token（默认关闭）。

> **这只是防爬，不是保密。** 任何人打开首页都能拿到 Cookie，而且同样的数据就在本公开仓库的 `docs/` 里。需要真正的访问控制，请用 Cloudflare Access 或把仓库设为私有。

## 工作原理

```text
GitHub Actions（cron 17 */3 * * * UTC，也可手动触发）
 │
 ├─ build_dataset.py
 │   ├─ ① 收集      文本源 + 域名源 + allowlist + 当前 DNS IP，剔除 Cloudflare 官方段和 denylist
 │   ├─ ② 预算      按优先级取前 PROXYIP_MAX_CANDIDATES（默认 1400）
 │   ├─ ③ 验证      api.090227.xyz/check：出口 IPv4、国家、ASN、colo、bot score
 │   ├─ ④ 测速排序  本机 TCP+TLS RTT → 7 天稳定性 → 前 30 个下载 10MB 测吞吐 → 叠加本地探测
 │   ├─ ⑤ 过滤      只留目标国家（默认 US），同一 ASN 最多 10 个（主 IP 永远保留）
 │   ├─ ⑥ 选主      主 IP 健康就保留，连续失败才 failover
 │   └─ ⑦ 输出      docs/*.txt / *.json、ip_history.json
 ├─ validate_outputs.py + unittest
 └─ scripts/auto_update.py
     ├─ sync_kv.py        Cloudflare REST API 写 13 个 KV key
     ├─ sync_dns.py       Cloudflare REST API 把 A 记录改成主 IP
     ├─ wrangler deploy   每次运行都会部署（不管 worker.js 有没有改动）
     ├─ 线上验证 + audit.py
     └─ git commit/push docs/ 数据快照

Cloudflare：Worker cf-proxyip-us（只读 KV 分发，不做扫描）· KV PROXYIP_KV · DNS-only A proxyip.leilaomi.cc.cd
```

## 候选来源

只收第三方中转 ProxyIP，下载 `https://www.cloudflare.com/ips-v4` 后剔除 Cloudflare 自身的 IP 段。

| 来源 | 类型 | 说明 |
|---|---|---|
| `zip.cm.edu.kg/all.txt` | 文本 | 只取标记 `#US`、端口 443 的行，候选主力 |
| `ProxyIP.US.CMLiussss.net` | 域名 A 记录 | 带 US 地区提示 |
| `ProxyIP.{SG,JP,HK,KR,DE,SE,NL,FI,GB}.CMLiussss.net` | 域名 | 地区不在 `PROXYIP_TARGET_COUNTRIES` 内时**直接跳过**，不占检测预算 |
| `cdn.xn--b6gac.eu.org`、`cdn-all.xn--b6gac.eu.org` | 域名 | 没有地区信息 |
| `edgetunnel.anycast.eu.org`、`ts.hpc.tw`、`bestproxy.onecf.eu.org` | 域名 | 保留在配置里，但近期没有 A 记录，`full.json` 的来源统计会记 `no A record` |
| `allowlist.txt` / `denylist.txt` | 手动 | 临时加入 / 排除，格式 `1.2.3.4`、`1.2.3.4:443`、`1.2.3.4:443#US` |
| 当前 DNS 主 IP | 自动 | 永远加入候选，保证主 IP 每轮都被检测 |

每个来源的条数、接受数和错误都记录在 `full.json` 的 `summary.sources`。

## 候选预算

候选总数通常超过 `PROXYIP_MAX_CANDIDATES`（默认 1400），超出部分按以下优先级截断：

1. 当前主 IP
2. 近 8 次检测内成功过的 IP（来自 `docs/ip_history.json`）
3. 带目标地区提示的 IP（`#US` 或 `ProxyIP.US.*`）
4. 其余按 `sha256(UTC日期|IP)` 排序：每天换一批，几天内能把整个池子轮一遍

> 2026-10 之前是按 IP 字符串排序后截断，每轮都会固定丢掉 `216.x` 之后约 30% 的地址段。

## 验证

- 每个候选调用 `https://api.090227.xyz/check?proxyip=<ip>`（并发 24，超时 35 秒），读取出口 IP 的国家、ASN、colo 和 Cloudflare bot management 字段。
- 只接受 `success=true` **且** `supports_ipv4=true`。出口走 IPv6 的 `ipv6_only` 代理目前会被丢弃（通常约占验证成功数的一半）。
- 检测 API 超时或网络错误时，才会退回直连 HTTPS 探测（`direct_https`）。这类结果标记 `fallback_unverified`、加 200000 排序惩罚、不能成为健康主 IP，也不计入 7 天成功率。

## 排序与测速：四个阶段

排序先过质量门槛，再按有效延迟升序。门槛内 bot score 不再加分，只在速度完全相同时做并列参考。四个阶段各提供排序需要的一项输入：

| 阶段 | 提供的输入 | 覆盖范围 | 代价 |
|---|---|---|---|
| 1 | 本地 RTT 与抖动 | 全部通过验证的候选 | 低，只握手不传数据 |
| 2 | 7 天成功率与历史平均延迟 | 已被巡检过的 IP | 免费，读 `docs/ip_history.json` |
| 3 | 真实下载吞吐 | 阶段 1 筛出的 Top N + 当前主 IP | 高，每个 IP 下载 10MB |
| 4 | 本机探测数据 | 可选 | 由本机承担 |

### 阶段 1：本地 RTT 与抖动

本机对每个通过验证的候选做 `PROXYIP_PROBE_SAMPLES` 次 TCP + TLS 握手采样，取 P50 作为 `latency_ms`，取 max - min 作为 `rtt_jitter_ms`。这是不依赖第三方测速 API 的真实首跳成本；第三方检测 API 的往返时间单独保留在 `api_latency_ms`。

### 质量门槛

以下任一不满足就排到后面：

- bot score 低于 `PROXYIP_CURRENT_MIN_BOT_SCORE`，或命中 corporateProxy / verifiedBot
- `direct_https` 兜底路径（未验证出口，bot score 是硬编码值）
- P50 延迟高于 `PROXYIP_CURRENT_MAX_LATENCY_MS`
- 抖动高于 `PROXYIP_MAX_JITTER_MS`
- 有效样本不足 `PROXYIP_PROBE_MIN_SAMPLES` 次 —— 3 次握手失败 2 次时只剩 1 个样本，抖动会被算成 0，不单独判负的话测量失败反而会伪装成最稳定
- 出口不在 `PROXYIP_TARGET_COUNTRIES`
- 7 天成功率低于 `PROXYIP_MIN_SUCCESS_RATE_7D`

### 阶段 2：滚动稳定性历史

有效成功率来自 `docs/ip_history.json` 的滚动窗口。历史不足 `PROXYIP_MIN_HISTORY_CHECKS` 次时按 `PROXYIP_HISTORY_UNKNOWN_RATE` 收缩，新候选既不会白捡满分，也不会因为没历史被质量门槛直接判负。

`docs/ip_history.json` 只保留 `PROXYIP_HISTORY_WINDOW` 次结果和 `PROXYIP_HISTORY_RETENTION_DAYS` 天内出现过的 IP，记录的是「验证通过且本机握手成功」，不受地区过滤和 ASN 截断影响。CI 必须把它一起提交，否则历史无法跨运行累积。吞吐另存 `mbps` / `mbps_at`，超过 `PROXYIP_THROUGHPUT_MAX_AGE_HOURS` 就不再作为历史兜底。

### 阶段 3：两级筛选与真实吞吐

候选池全部下载测速太贵，所以先用阶段 1 的廉价 RTT 筛出 Top `PROXYIP_THROUGHPUT_TOP_N`（外加当前主 IP，它无论排多少都必须测），再对这个小池子经 ProxyIP 真实下载 `speed.cloudflare.com/__down`，记录 MB/s。单看 RTT 会选出「握手快但传输慢」的 IP，所以吞吐折算成传输耗时直接进有效延迟；没测到速的按 `PROXYIP_THROUGHPUT_UNKNOWN_MS` 计，排在所有测过速的后面。实测差异通常在 0.002–3.8 MB/s 之间，吞吐因此是排序的主导项。

当前主 IP 还额外受 `PROXYIP_THROUGHPUT_MIN_MBPS` 约束：测到的吞吐低于它，连续 `PROXYIP_FAILOVER_THRESHOLD` 次就 failover。

### 有效延迟公式

过门槛的按有效延迟升序排：

```
有效延迟 = 0.5 × 本次 P50 + 0.5 × 历史平均延迟
         + PROXYIP_JITTER_WEIGHT × 抖动
         + (1 - 有效成功率) × PROXYIP_STABILITY_PENALTY_MS
         + 采样缺口 × PROXYIP_PROBE_SAMPLE_DEFICIT_PENALTY_MS
         + PROXYIP_THROUGHPUT_WEIGHT × 传 PROXYIP_THROUGHPUT_BYTES 的耗时
```

### 阶段 4：本地探测叠加

GitHub Actions 是单点数据，「最快」只对美西有效。`PROXYIP_PROBE_ONLY=1` 让你在自己实际使用的机器上跑一次探测，产出 `docs/probe_local.json`，CI 读到后按权重并进排序：

- RTT 按 `PROXYIP_LOCAL_PROBE_WEIGHT` 加权合并；抖动取两边更差的那个。
- 本地探测补不上 CI 侧的握手时，用本地结果顶上，采样缺口按两边较大值算。
- 吞吐按 `PROXYIP_THROUGHPUT_CI_WEIGHT`（CI）与 `PROXYIP_LOCAL_THROUGHPUT_WEIGHT`（本地）加权，本地默认更高，因为它才是你真实使用的网络。
- 文件超过 `PROXYIP_LOCAL_PROBE_MAX_AGE_HOURS` 就整份丢弃，过期数据不会污染排序。

```bash
export PROXYIP_PROBE_ONLY=1
python3 build_dataset.py          # 只写 docs/probe_local.json，不碰 state / history / ip_history
git add docs/probe_local.json && git commit -m "Refresh local probe" && git push
```

不提交 `docs/probe_local.json` 的话，CI 读不到，等于只有美西单点数据。该文件由本地运行负责提交，CI 的 `git add` 列表里没有它，不会被覆盖。

## 主 IP 与 failover

目标是稳定，不是追求最快。

| 情况 | 行为 |
|---|---|
| 主 IP 通过验证且满足主 IP 门槛（bot score ≥ 80、延迟 ≤ 2500 ms、吞吐 ≥ 1 MB/s、仍在 US、稳定性达标、不是 fallback） | 保留，`status=healthy`，`failure_count` 清零 |
| 主 IP 质量不达标，但距上次切换不到 6 小时 | 保留，`status=degraded_quality_cooldown` |
| 主 IP 没通过验证 | 保留，`status=degraded`，`failure_count+1`；沿用上一轮的国家/ASN 并标记 `risk.stale=true`，审计放行，这样计数才会被提交 |
| 连续失败达到 `PROXYIP_FAILOVER_THRESHOLD`（默认 2） | 切到排名第一的候选，写入 `history.json` |

- **不会主动升级**：主 IP 只要健康，即使排名已经掉到中游、前面有快得多的候选，也不会切换。
- 按实际调度间隔（见下一节），一个失效的主 IP 通常要等两轮、也就是十几个小时才会被换掉。客户端可以用 `/standby.txt` 自己做兜底。
- DNS 永远只发布 1 个 IP；备用池只通过 Worker 分发。

## 自动化与部署

`.github/workflows/proxyip-auto-update.yml`，job 超时 45 分钟，`concurrency` 不并发。

- **设计**：每 3 小时一次（`17 */3 * * *` UTC）。
- **实际**：GitHub 的定时任务经常延迟或被跳过，实测平均约 6–7 小时一轮，最长超过 10 小时。急用时去 Actions 页面手动 *Run workflow*。
- 合并到 `main` **不会**触发运行（没有 push 触发器），新代码在下一次运行时生效。
- 每次运行顺序：语法检查 → `build_dataset.py` → 校验 + 单元测试 → `auto_update.py`（同步 KV → 同步 DNS → `wrangler deploy` → 线上验证 → `audit.py` → 按白名单提交 `docs/`）。
- 线上写入发生在审计和 git push **之前**：如果审计或推送失败，线上已经更新，但仓库里的 `state.json` 是旧的。
- 一轮通常 3–5 分钟，测速约经第三方代理下载 300MB。

## 配置

| 项 | 值 / 位置 |
|---|---|
| Worker 名 | `cf-proxyip-us`（`wrangler.toml`，`workers_dev=false`） |
| 自定义域名 `list.leilaomi.cc.cd` | 在 Cloudflare 控制台绑定，**不在** `wrangler.toml` 里 |
| KV binding | `PROXYIP_KV`（namespace id 在 `wrangler.toml`） |
| Zone / 记录 | `PROXYIP_ZONE_NAME=leilaomi.cc.cd`、`PROXYIP_RECORD_NAME=proxyip.leilaomi.cc.cd` |

**GitHub Secrets**

| Secret | 用途 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | KV / DNS REST API + `wrangler deploy`。需要的权限：Workers Scripts 编辑、Workers KV 编辑、Zone DNS 编辑 |
| `PROXYIP_HMAC_SECRET` | CI 生成 Token 做线上验证 |

**Worker Secret**

| Secret | 用途 |
|---|---|
| `PROXYIP_SECRET` | 签发 / 校验 Cookie 和 Token。**值必须与 `PROXYIP_HMAC_SECRET` 相同**（两边名字不同是历史原因） |

`CLOUDFLARE_ACCOUNT_ID` 作为普通环境变量写在 workflow 里，它不是密钥。

**KV key**（`docs/kv-manifest.json`）：`result_json`、`current_json`、`current_txt`、`standby_txt`、`top5_txt`、`all_txt`、`us_txt`、`best_txt`、`base64_txt`、`v2ray_txt`、`history_json`、`ip_history_json`、`state_json`。Worker 实际只读前面的文本 key 和 `result_json`，后 3 个是备份。

## 环境变量

门槛、排序权重、failover、同步与本机探测都可以通过环境变量覆盖。GitHub Actions 里设置的值见 `scripts/auto_update.py` 和 `.github/workflows/proxyip-auto-update.yml`；未设置时用下表默认值。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROXYIP_TARGET_COUNTRIES` | `US` | 目标出口国家，可用逗号分隔 |
| `PROXYIP_PREFERRED_COLOS` | `IAD` | 同等风险下优先的 Cloudflare colo |
| `PROXYIP_RECORD_NAME` | `proxyip.leilaomi.cc.cd` | DNS-only A 记录名 |
| `PROXYIP_MAX_WORKERS` | `24` | 候选验证并发数 |
| `PROXYIP_CHECK_TIMEOUT` | `35` | 单个候选检测超时，单位秒 |
| `PROXYIP_MAX_CANDIDATES` | `1400` | 最多检测多少个候选 |
| `PROXYIP_LIST_DOMAIN` | `https://list.leilaomi.cc.cd` | `scripts/auto_update.py` 读取线上列表的地址 |
| `PROXYIP_SKIP_GENERATE` | `0` | 置 `1` 时跳过 `build_dataset.py`，直接同步已生成的 `docs/`（CI 用） |
| `PROXYIP_FAILOVER_THRESHOLD` | `2` | 当前主 IP 连续几次不过线就 failover |
| `PROXYIP_CURRENT_MIN_BOT_SCORE` | `80` | 当前主 IP 最低 bot score 门槛 |
| `PROXYIP_CURRENT_MAX_LATENCY_MS` | `2500` | 当前主 IP 最大延迟门槛 |
| `PROXYIP_SWITCH_COOLDOWN_HOURS` | `6` | 切换冷却时间，单位小时 |
| `PROXYIP_PROBE_SAMPLES` | `3` | 每个候选的本地握手采样次数 |
| `PROXYIP_PROBE_TIMEOUT` | `5` | 单次握手超时，单位秒 |
| `PROXYIP_PROBE_MIN_SAMPLES` | `2` | 过门槛要求的最少有效样本数 |
| `PROXYIP_PROBE_SAMPLE_DEFICIT_PENALTY_MS` | `2000` | 每缺 1 个有效样本折算的惩罚毫秒 |
| `PROXYIP_MAX_JITTER_MS` | `500` | 过门槛允许的最大抖动 |
| `PROXYIP_JITTER_WEIGHT` | `2` | 抖动在有效延迟里的权重，单位毫秒 |
| `PROXYIP_STABILITY_PENALTY_MS` | `4000` | 有效成功率从 100% 降到 0% 时最多加多少毫秒 |
| `PROXYIP_MIN_SUCCESS_RATE_7D` | `0.9` | 过门槛允许的最低 7 天成功率 |
| `PROXYIP_MIN_HISTORY_CHECKS` | `6` | 成功率视为可信所需的最少历史次数 |
| `PROXYIP_HISTORY_UNKNOWN_RATE` | `0.85` | 历史不足时的收缩成功率 |
| `PROXYIP_HISTORY_LAT_WINDOW` | `8` | 历史平均延迟取最近多少次的均值 |
| `PROXYIP_HISTORY_WINDOW` | `56` | 每个 IP 保留的结果次数 |
| `PROXYIP_HISTORY_RETENTION_DAYS` | `7` | 未被巡检到多少天后清理该 IP |
| `PROXYIP_THROUGHPUT_TOP_N` | `30` | 廉价 RTT 筛出多少个候选做真实下载测速 |
| `PROXYIP_THROUGHPUT_BYTES` | `10485760` | 测速下载的字节数 |
| `PROXYIP_THROUGHPUT_TIMEOUT` | `12` | 单个 IP 吞吐测试总超时，单位秒 |
| `PROXYIP_THROUGHPUT_WORKERS` | `1` | 吞吐并发数；共享带宽会互相拖慢，默认串行保证可比性 |
| `PROXYIP_THROUGHPUT_ABORT_S` | `4` | 采样满多少秒后仍低于门槛就提前放弃 |
| `PROXYIP_THROUGHPUT_ABORT_MBPS` | `0.4` | 提前放弃的吞吐门槛，单位 MB/s |
| `PROXYIP_THROUGHPUT_WEIGHT` | `1.0` | 传输耗时在有效延迟里的权重 |
| `PROXYIP_THROUGHPUT_UNKNOWN_MS` | `20000` | 未测速时按多少毫秒计，保证排在测过速的后面 |
| `PROXYIP_THROUGHPUT_MIN_MBPS` | `1.0` | 当前主 IP 的最低吞吐门槛，单位 MB/s |
| `PROXYIP_THROUGHPUT_MAX_AGE_HOURS` | `72` | 历史吞吐多久后不再兜底 |
| `PROXYIP_THROUGHPUT_CI_WEIGHT` | `0.4` | 吞吐合并时 CI 实测的权重 |
| `PROXYIP_PROBE_ONLY` | `0` | 置 `1` 只在本机探测，产出 `docs/probe_local.json` |
| `PROXYIP_LOCAL_PROBE_WEIGHT` | `0.6` | RTT 合并时本机数据的权重 |
| `PROXYIP_LOCAL_THROUGHPUT_WEIGHT` | `0.6` | 吞吐合并时本机数据的权重 |
| `PROXYIP_LOCAL_PROBE_MAX_AGE_HOURS` | `168` | `docs/probe_local.json` 超过多少小时就不再采信 |

## 本地运行

Windows 上把 `python3` 换成 `python`，`scripts/*.py` 需要手动展开。

```bash
# 只生成数据并校验，不碰线上
python3 build_dataset.py
python3 scripts/validate_outputs.py
python3 -m unittest discover -s tests
node --check worker.js

# 阶段 4：本机探测（约 6 分钟），只写 docs/probe_local.json
PROXYIP_PROBE_ONLY=1 python3 build_dataset.py
git add docs/probe_local.json && git commit -m "Refresh local probe" && git push

# 完整流程：会改线上 KV / DNS / Worker，没有明确需要不要跑
export CLOUDFLARE_ACCOUNT_ID="<Cloudflare Account ID>"
export CLOUDFLARE_API_TOKEN="<Cloudflare API Token>"
export PROXYIP_HMAC_SECRET="<HMAC Secret>"
python3 scripts/auto_update.py
```

`docs/probe_local.json` 超过 168 小时就不会再被采用，这时排序只反映 GitHub 美国 runner 的网络。要让阶段 4 生效，需要至少每周在自己的网络上跑一次。

## 已知限制

- **检测单点**：所有验证都依赖 `api.090227.xyz`。它不可用时只剩未验证的直连探测，这类结果不会成为主 IP。
- **IPv6 出口被丢弃**：`ipv6_only` 代理通常也能当 ProxyIP 用，但目前不收。
- **测速视角**：RTT 和吞吐是从 GitHub 美国 runner 测的；不跑阶段 4，排序就和你本地的体验无关。
- **主 IP 不择优**：见上文 failover。
- **调度延迟**：见上文自动化。
- **每轮都部署**：`wrangler deploy` 无条件执行。
- **ETag**：只有读取 `result_json` 的路径（如 `/current.json`、`/full.json`）支持 `If-None-Match` → 304；文本路径走 KV 快速通道，只带 ETag，不返回 304。
- **`audit.py` 仍调用 `wrangler kv key get`** 读 KV，和同步走 REST API 不一致。
- **仓库膨胀**：每轮都提交 `full.json` 和约 580KB 的 `ip_history.json`。
- Actions 没有 pin 到 commit SHA，`wrangler@4` 也没锁小版本。

## 排障指南

### 1. Action 在 KV 同步阶段失败

常见旧报错：

```text
A request to the Cloudflare API (/memberships) failed. Authentication error [code: 10000]
A request to the Cloudflare API (/accounts) failed. Invalid access token [code: 9109]
```

当前修复方式：

- `scripts/sync_kv.py` 已改为 Cloudflare REST API。
- workflow 必须传入 `CLOUDFLARE_ACCOUNT_ID`。
- 仓库必须配置 `CLOUDFLARE_API_TOKEN` Secret。

### 2. Action 在 DNS 同步阶段失败

检查：

- API Token 是否有 Zone DNS 编辑权限。
- `PROXYIP_ZONE_NAME` 是否正确。
- `PROXYIP_RECORD_NAME` 是否属于该 Zone。

### 3. Worker 部署失败

检查：

- API Token 是否有 Workers Scripts 编辑权限。
- `wrangler.toml` 的 Worker 名称、KV binding、route/custom domain 是否正确。
- GitHub Actions 中 `wrangler@4` 是否安装成功。

### 4. Token 认证失败

检查：

- GitHub Secret `PROXYIP_HMAC_SECRET` 是否存在。
- Cloudflare Worker Secret `PROXYIP_SECRET` 是否存在。
- 两者值是否一致。
- 用的是不是 curl 等被拦截的 UA：这类 UA 只认 Bearer Token，不认 Cookie。
- 跨 UTC 零点：前一天的 Token 仍有效，再往前就会被拒绝。

### 5. Actions 变红但线上没切换

先看 `docs/state.json` 的 `status` 和 `failure_count`。`degraded` 表示主 IP 本轮验证失败，下一轮仍失败就会 failover。如果 `failure_count` 一直不涨，说明 `audit.py` 或 git push 失败了，计数没有被提交。

## 仓库结构

| 路径 | 作用 |
|---|---|
| `build_dataset.py` | 收集、预算、验证、四阶段排序、failover、写 `docs/` |
| `worker.js` | Worker：路由、认证、KV 读取、状态页 |
| `wrangler.toml` | Worker 名、KV binding |
| `scripts/auto_update.py` | CI 编排：同步、部署、验证、审计、提交 |
| `scripts/sync_kv.py` / `sync_dns.py` | Cloudflare REST API 同步 KV / DNS |
| `scripts/validate_outputs.py` | `docs/` 一致性校验（提交前） |
| `scripts/audit.py` | 部署后审计：文件一致性、DNS 解析、线上接口、认证 |
| `tests/` | 排序、历史、failover、候选预算、ASN、Worker 认证的单元测试 |
| `allowlist.txt` / `denylist.txt` | 手动候选（可选） |
| `docs/` | 每轮的数据快照，由脚本生成，**不要手改** |
| `docs/probe_local.json` | 本机探测结果，只由本地提交，CI 不覆盖 |
| `docs/operations.md` | 运维说明 |
| `docs/archive/` | 2026-06 审计与计划，只作历史参考 |
| `AGENTS.md` | 维护约定和线上操作禁令 |

不进仓库：`docs/full.slim.json`（KV 上传前的瘦身版）、根目录 `result.json`（含 `all_results` 的调试输出）。

## 历史与归档

KV 同步曾用 `wrangler kv key put`，在 CI 中因 Wrangler 额外请求 `/memberships`、`/accounts` 返回认证错误而失败。现已改为 `scripts/sync_kv.py` 走 Cloudflare REST API，workflow 传入 `CLOUDFLARE_ACCOUNT_ID`，GitHub Secret `CLOUDFLARE_API_TOKEN` 已更新。排障见[第 1 节](#1-action-在-kv-同步阶段失败)。

2026-06 的审计报告与分阶段计划已全部落地，归档在 `docs/archive/`：

- [docs/archive/audit-2026-06-04.md](docs/archive/audit-2026-06-04.md)：项目改进建议报告。
- [docs/archive/implementation-plan-2026-06-04.md](docs/archive/implementation-plan-2026-06-04.md)：当时的分阶段落地计划。注意它指的是安全基线、访问控制、数据质量、Worker 性能，与本文[排序与测速：四个阶段](#排序与测速四个阶段)不是同一件事。
- [docs/archive/continue-2026-06-04.md](docs/archive/continue-2026-06-04.md)：历史延续记录。

日常运行与部署以 [docs/operations.md](docs/operations.md) 和本文为准，不要从归档文档推断当前行为。
