# cf-proxyip-us

`cf-proxyip-us` 是一个面向 Cloudflare Worker 的 ProxyIP 自动维护项目。它会定时收集、检测、排序美国方向的 ProxyIP 候选，选择 1 个稳定主 IP 写入 DNS-only A 记录，并通过 Worker + KV 分发当前 IP、备用池、完整报告和订阅数据。

本文描述的是仓库当前实际实现：候选按「本地 RTT → 滚动稳定性历史 → 真实下载吞吐 → 本地探测叠加」四个阶段排序，KV 同步走 Cloudflare REST API。动态数据一律以线上接口和 `docs/` 为准，本文不写死候选数量、延迟或当前 IP。

目录：

- [当前线上地址](#当前线上地址)
- [项目实际架构](#项目实际架构)
- [仓库结构](#仓库结构)
- [数据产物](#数据产物)
- [认证方式](#认证方式)
- [客户端使用方式](#客户端使用方式)
- [Cloudflare 配置](#cloudflare-配置)
- [GitHub Actions 自动巡检](#github-actions-自动巡检)
- [KV 同步方式](#kv-同步方式)
- [DNS 同步方式](#dns-同步方式)
- [本地重新生成数据](#本地重新生成数据)
- [候选源策略](#候选源策略)
- [Failover 策略](#failover-策略)
- [排序与测速：四个阶段](#排序与测速四个阶段)
- [环境变量](#环境变量)
- [安全与反爬](#安全与反爬)
- [排障指南](#排障指南)
- [历史与归档](#历史与归档)

## 当前线上地址

| 用途 | 地址 | 说明 |
|---|---|---|
| Worker 分发首页 | `https://list.leilaomi.cc.cd/` | 首页、认证入口、API 分发入口 |
| 实际 ProxyIP 域名 | `proxyip.leilaomi.cc.cd` | DNS-only / 灰云，永远只指向 1 个当前主 IP |
| 当前主 IP | `https://list.leilaomi.cc.cd/current.txt` | 客户端实际使用的主 ProxyIP |
| 当前主 IP 详情 | `https://list.leilaomi.cc.cd/current.json` | 当前主 IP、质量、状态信息 |
| 备用候选 | `https://list.leilaomi.cc.cd/standby.txt` | 主 IP 失效后的备用池 |
| 推荐 Top 5 | `https://list.leilaomi.cc.cd/top5.txt` | 当前主 IP + 前 4 个备用候选 |
| 全量列表 | `https://list.leilaomi.cc.cd/all.txt` | 通过检测的候选 IP |
| US 列表 | `https://list.leilaomi.cc.cd/us.txt` | 当前项目默认目标地区列表 |
| Top 20 | `https://list.leilaomi.cc.cd/best.txt` | 排名前 20 的候选 |
| 完整报告 | `https://list.leilaomi.cc.cd/full.json` | 检测摘要与公开报告 |
| V2Ray Base64 | `https://list.leilaomi.cc.cd/v2ray.txt` | Base64 编码 IP 列表，不是完整节点订阅 |
| 获取 Token | `https://list.leilaomi.cc.cd/token` | 需先访问首页拿 Cookie |
| 健康检查 | `https://list.leilaomi.cc.cd/health` | 最小公开健康状态 |
| 详细健康检查 | `https://list.leilaomi.cc.cd/health/full` | 需要 Cookie 或 HMAC Token |
| 统计数据 | `https://list.leilaomi.cc.cd/stats` | 需要 Cookie 或 HMAC Token |

## 项目实际架构

```text
GitHub Actions（每 3 小时，cron 17 */3 * * * UTC）
  ├─ build_dataset.py                 # 收集 -> 验证 -> 四阶段排序 -> failover -> 写 docs/
  │    ├─ check_cmliu / 直连 HTTPS      # 第三方验证出口、地区、bot score
  │    ├─ probe_rtt                     # 本机 TCP+TLS 多次采样 -> P50 + 抖动
  │    ├─ merge_local_probe             # 合并 docs/probe_local.json（存在且未过期才用）
  │    ├─ probe_throughput              # 廉价 RTT 筛出 Top N 后真实下载测速
  │    └─ update_ip_history             # 滚动 7 天历史写回 docs/ip_history.json
  ├─ scripts/validate_outputs.py      # 校验 docs 输出一致性
  ├─ python3 -m unittest discover -s tests
  ├─ scripts/auto_update.py           # PROXYIP_SKIP_GENERATE=1：同步 + 部署 + 线上验证
  │    ├─ scripts/sync_kv.py            # Cloudflare REST API 写入 KV
  │    ├─ scripts/sync_dns.py           # Cloudflare REST API 同步 DNS-only A 记录
  │    ├─ wrangler deploy               # 部署 Worker
  │    └─ scripts/audit.py              # 运行结果审计
  └─ 按白名单提交 docs/ 数据快照

本机（可选，PROXYIP_PROBE_ONLY=1）
  └─ build_dataset.py -> docs/probe_local.json -> 提交仓库 -> CI 按权重合并

Cloudflare
  ├─ Worker: cf-proxyip-us           # 分发页面、API、认证与缓存
  ├─ KV Namespace                    # 保存 current/all/full/top5 等数据
  └─ DNS-only A: proxyip.leilaomi.cc.cd -> 当前主 IP
```

关键点：

- Worker 只负责分发、认证、读取 KV、返回 API，不负责在线扫描 IP。
- 候选收集、检测、四阶段排序、failover 全部在 GitHub Actions 里完成。
- 本机探测只是可选的加权数据源；`docs/probe_local.json` 缺失时 CI 照常运行。
- DNS 始终只写入 1 条 DNS-only A 记录，避免出口频繁跳变。
- KV 同步使用 Cloudflare REST API，不再使用 `wrangler kv key put`。
- Worker 部署仍使用 `wrangler deploy`。

## 仓库结构

| 路径 | 作用 |
|---|---|
| `build_dataset.py` | 候选收集、验证、四阶段探测排序、failover、生成 `docs/` |
| `worker.js` | Worker 路由、认证、API、KV 读取、公开状态页 |
| `wrangler.toml` | Worker、KV binding、route 配置 |
| `scripts/auto_update.py` | CI 自动流程：同步 KV/DNS、部署 Worker、线上验证、审计、提交 |
| `scripts/sync_kv.py` | 通过 Cloudflare REST API 写 KV |
| `scripts/sync_dns.py` | 通过 Cloudflare REST API 同步 DNS-only A 记录 |
| `scripts/validate_outputs.py` | `docs/` 输出一致性校验 |
| `scripts/audit.py` | 部署后审计 |
| `tests/` | 单元测试：排序逻辑、历史窗口、输出校验、Worker 静态检查 |
| `.github/workflows/proxyip-auto-update.yml` | GitHub Actions 定时入口 |
| `docs/` | 每次巡检的可审计数据快照 |
| `docs/operations.md` | 运行与部署说明、所需权限、排障 |
| `docs/archive/` | 2026-06 的审计报告与阶段计划，已落地归档 |
| `AGENTS.md` | 维护约定、必跑验证命令、线上操作禁令 |

## 数据产物

仓库 `docs/` 保留每次自动巡检后的可审计快照。

| 文件 | 说明 |
|---|---|
| `docs/current.txt` | 当前稳定主 IP，供 `proxyip.leilaomi.cc.cd` 使用 |
| `docs/current.json` | 当前主 IP 的详细状态 |
| `docs/state.json` | failover 状态、连续失败次数、最近成功时间 |
| `docs/history.json` | 主 IP 切换历史 |
| `docs/ip_history.json` | 每个 IP 的滚动稳定性窗口，排序和主 IP 质量判定的输入 |
| `docs/probe_local.json` | `PROXYIP_PROBE_ONLY=1` 在本机产出的 RTT + 吞吐数据，CI 按权重合并；只由本地运行提交 |
| `docs/standby.txt` | 备用候选池 |
| `docs/top5.txt` | 当前主 IP + 前 4 个备用候选 |
| `docs/all.txt` | 通过 IPv4 与目标地区检测的全部候选 |
| `docs/us.txt` | 当前目标地区 US 列表 |
| `docs/best.txt` | 排名前 20 的候选 |
| `docs/dns-records.json` | 期望同步到 Cloudflare 的 DNS A 记录 |
| `docs/full.json` | 完整公开报告，已剔除 debug 用的 `all_results` 与 `throughput_by_ip` |
| `docs/kv-manifest.json` | KV key 与本地文件映射 |
| `docs/v2ray.txt` | Base64 编码 IP 列表 |
| `docs/operations.md` | 运行与部署说明、所需权限、排障 |
| `docs/archive/` | 2026-06 的历史审计与阶段计划，已落地归档 |

以下文件由脚本生成但不进仓库（已在 `.gitignore`）：

| 文件 | 来源 | 说明 |
|---|---|---|
| `docs/full.slim.json` | `scripts/sync_kv.py` | 上传 KV `result_json` 前生成的瘦身版，剔除 `all_results` |
| `result.json`（仓库根） | `build_dataset.py` | 含 `all_results` 的完整调试输出 |

动态数据以线上接口和 `docs/` 文件为准，README 不写死候选数量、延迟、当前 IP。

## 认证方式

数据接口默认需要认证，支持 Cookie 和 HMAC Token。

### 1. Cookie 认证

浏览器访问首页会自动设置：

```text
proxyip_access=ok
```

有效期 8 小时。

### 2. HMAC Token 认证

推荐程序化访问使用 Header，不建议把 token 放 URL。

```bash
# 1. 访问首页获取 Cookie
curl -c cookies.txt https://list.leilaomi.cc.cd/

# 2. 使用 Cookie 获取 HMAC Token
curl -b cookies.txt https://list.leilaomi.cc.cd/token

# 3. 使用 Bearer Token 访问数据接口
curl -H "Authorization: Bearer <HMAC_TOKEN>" https://list.leilaomi.cc.cd/all.txt
```

Token 格式：

```text
YYYYMMDD-HMAC-SHA256-Hex
```

Worker 端必须配置 `PROXYIP_SECRET`。它应与 GitHub Actions 的 `PROXYIP_HMAC_SECRET` 保持一致。

## 客户端使用方式

客户端、面板或 VLESS Worker 里的 ProxyIP 字段填写：

```text
proxyip.leilaomi.cc.cd
```

不要填写 `list.leilaomi.cc.cd`。

区别：

| 域名 | 用途 |
|---|---|
| `proxyip.leilaomi.cc.cd` | 实际 ProxyIP 出口域名，DNS-only 单 A 记录 |
| `list.leilaomi.cc.cd` | Worker 数据分发域名，提供 API、报告、Token |

## Cloudflare 配置

当前实际配置：

| 配置项 | 当前值 / 说明 |
|---|---|
| Worker 名称 | `cf-proxyip-us` |
| Worker 配置 | `wrangler.toml` |
| Worker 分发域名 | `list.leilaomi.cc.cd` |
| ProxyIP DNS-only 域名 | `proxyip.leilaomi.cc.cd` |
| Cloudflare Account ID | 通过 GitHub Actions 环境变量 `CLOUDFLARE_ACCOUNT_ID` 传入 |
| Cloudflare API Token | GitHub Secret：`CLOUDFLARE_API_TOKEN` |
| HMAC Secret | GitHub Secret：`PROXYIP_HMAC_SECRET`；Worker Secret：`PROXYIP_SECRET` |

> 不要把 Cloudflare API Token 明文写入 README、workflow、脚本或提交历史。只允许放在 GitHub Secrets / Cloudflare Secrets 中。

### 必需 GitHub Secrets

| Secret | 用途 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | GitHub Actions 调用 Cloudflare REST API、DNS API 和 Wrangler 部署 |
| `PROXYIP_HMAC_SECRET` | GitHub Actions 生成 HMAC Token，用于线上验证 |

### 必需 Worker Secret

| Secret | 用途 |
|---|---|
| `PROXYIP_SECRET` | Worker 端验证 HMAC Token，值应与 `PROXYIP_HMAC_SECRET` 一致 |

### GitHub Actions 环境变量

`.github/workflows/proxyip-auto-update.yml` 当前传入：

```yaml
env:
  CLOUDFLARE_ACCOUNT_ID: <Cloudflare Account ID>
  CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
  PROXYIP_HMAC_SECRET: ${{ secrets.PROXYIP_HMAC_SECRET }}
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

`CLOUDFLARE_ACCOUNT_ID` 是账号 ID，不是密钥；`CLOUDFLARE_API_TOKEN` 必须只放 Secret。

## GitHub Actions 自动巡检

自动化入口：

```text
.github/workflows/proxyip-auto-update.yml
```

触发方式：

- 定时：每 3 小时执行一次，Cron 为 `17 */3 * * *` UTC。
- 手动：GitHub 仓库 → Actions → ProxyIP Auto Update → Run workflow。

执行流程（job 超时 45 分钟）：

1. Checkout 仓库完整历史。
2. 安装 Node 20。workflow 写的是 `node-version: "20"`，但 GitHub 托管 runner 已弃用 Node 20，可能提示被强制升到更高版本；只要 `wrangler@4` 装得上即可。
3. 安装 `wrangler@4`。
4. 语法检查：
   - `python3 -m py_compile build_dataset.py scripts/*.py`
   - `node --check worker.js`
5. 生成、校验和测试输出：
   - `python3 build_dataset.py`
   - `python3 scripts/validate_outputs.py`
   - `python3 -m unittest discover -s tests`
6. 执行完整自动更新：
   - `python3 scripts/auto_update.py`（带 `PROXYIP_SKIP_GENERATE=1`，跳过上一步已做过的生成）
7. 自动同步 KV、DNS、部署 Worker、线上验证、必要时提交 `docs/` 数据快照。

## KV 同步方式

当前 `scripts/sync_kv.py` 使用 Cloudflare REST API 写 KV：

```text
PUT /client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}
```

它从以下位置读取配置：

| 配置 | 来源 |
|---|---|
| Account ID | 环境变量 `CLOUDFLARE_ACCOUNT_ID` |
| API Token | 环境变量 `CLOUDFLARE_API_TOKEN` |
| KV Namespace ID | `wrangler.toml` 的 KV namespace `id` |
| KV key 映射 | `docs/kv-manifest.json` |

这样做的原因：GitHub Actions 里使用 `wrangler kv key put` 时，Wrangler 会额外请求 Cloudflare `/memberships`、`/accounts`，某些 API Token 权限组合下会返回认证失败。REST API 直写 KV 更稳定、权限边界更清楚。

## DNS 同步方式

`scripts/sync_dns.py` 使用 Cloudflare REST API 同步 `PROXYIP_RECORD_NAME` 指定的 DNS-only A 记录。

默认值：

```bash
PROXYIP_ZONE_NAME=leilaomi.cc.cd
PROXYIP_RECORD_NAME=proxyip.leilaomi.cc.cd
```

脚本会确保最终只有 1 条 DNS-only A 记录指向当前主 IP。

## 本地重新生成数据

命令按 `python3` 写；Windows 上若没有 `python3` 命令，把 `python3` 换成 `python` 即可，glob（如 `scripts/*.py`）也要在 PowerShell 里手动展开成文件列表。

只生成本地数据，不同步 Cloudflare：

```bash
python3 build_dataset.py
python3 scripts/validate_outputs.py
python3 -m unittest discover -s tests
```

只在本机探测（约 6 分钟），产出本地 RTT 与吞吐供 CI 加权合并：

```bash
PROXYIP_PROBE_ONLY=1 python3 build_dataset.py
git add docs/probe_local.json && git commit -m "Refresh local probe" && git push
```

完整自动流程，包括 KV、DNS、Worker 部署和线上验证：

```bash
export CLOUDFLARE_ACCOUNT_ID="<Cloudflare Account ID>"
export CLOUDFLARE_API_TOKEN="<Cloudflare API Token>"
export PROXYIP_HMAC_SECRET="<HMAC Secret>"
python3 scripts/auto_update.py
```

注意：本地完整流程会操作线上 Cloudflare。没有明确需要时，不要随便执行。

## 候选源策略

本项目只收集第三方中转 ProxyIP 候选，不使用 Cloudflare 官方 IP 段、普通 CF 优选 IP 或明显偏 CDN 边缘的来源。

原因：本项目目标是给 Cloudflare Worker / CDN 场景提供可用 ProxyIP，而不是寻找 Cloudflare 自身边缘节点。

候选源分三类：

| 类型 | 说明 |
|---|---|
| 文本型 ProxyIP 源 | 远程文本直接提供 IP / IP:端口 |
| 域名型 ProxyIP 源 | 解析 ProxyIP 域名 A 记录后检测 |
| 手动源 | `allowlist.txt` / `denylist.txt`，用于临时保留或排除候选 |

`allowlist.txt` / `denylist.txt` 支持：

```text
1.2.3.4
1.2.3.4:443
1.2.3.4:443#US
```

脚本会下载 Cloudflare 官方 IPv4 段并排除命中的候选，避免误把 Cloudflare 自身 IP 写入 ProxyIP 池。

## Failover 策略

项目优先稳定，不追求频繁切换。

- 当前主 IP 仍健康时保持不变。
- 当前主 IP 连续失效或质量低于门槛后才切换。
- Top5 / standby 会按 ASN 分散选择，降低同一 ASN 集中风险。
- DNS 永远只发布 1 个当前主 IP，备用池只通过 Worker 分发。

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

## 安全与反爬

Worker 提供基础防护：

- `robots.txt` 禁止抓取。
- 响应头包含 `X-Robots-Tag: noindex,nofollow,noarchive`。
- 常见 bot、crawler、curl、wget、python-requests、扫描器 UA 默认 403。
- 带合法 `Authorization: Bearer <HMAC_TOKEN>` 的程序化请求优先通过。
- 文本与 JSON 数据接口需要 Cookie 或 HMAC Token。
- `/health` 只返回最小健康信息。
- `/health/full` 和 `/stats` 需要认证。
- 支持 ETag / 304，减少重复传输。

这不是强认证系统。如果需要更严格访问控制，建议改为固定私密 token 或 Cloudflare Access。

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

## 历史与归档

KV 同步曾用 `wrangler kv key put`，在 CI 中因 Wrangler 额外请求 `/memberships`、`/accounts` 返回认证错误而失败。现已改为 `scripts/sync_kv.py` 走 Cloudflare REST API，workflow 传入 `CLOUDFLARE_ACCOUNT_ID`，GitHub Secret `CLOUDFLARE_API_TOKEN` 已更新。排障见[第 1 节](#1-action-在-kv-同步阶段失败)。

2026-06 的审计报告与分阶段计划已全部落地，归档在 `docs/archive/`：

- [docs/archive/audit-2026-06-04.md](docs/archive/audit-2026-06-04.md)：项目改进建议报告。
- [docs/archive/implementation-plan-2026-06-04.md](docs/archive/implementation-plan-2026-06-04.md)：当时的分阶段落地计划。注意它指的是安全基线、访问控制、数据质量、Worker 性能，与本文[排序与测速：四个阶段](#排序与测速四个阶段)不是同一件事。
- [docs/archive/continue-2026-06-04.md](docs/archive/continue-2026-06-04.md)：历史延续记录。

日常运行与部署以 [docs/operations.md](docs/operations.md) 和本文为准，不要从归档文档推断当前行为。
