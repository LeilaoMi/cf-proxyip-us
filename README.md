# cf-proxyip-us

`cf-proxyip-us` 是一个面向 Cloudflare Worker 的 ProxyIP 自动维护项目。它会定时收集、检测、排序美国方向的 ProxyIP 候选，选择 1 个稳定主 IP 写入 DNS-only A 记录，并通过 Worker + KV 分发当前 IP、备用池、完整报告和订阅数据。

> 当前仓库说明已按实际 CI / Cloudflare 同步方式更新：KV 同步已改为 Cloudflare REST API，避免 GitHub Actions 中 `wrangler kv key put` 因 `/memberships`、`/accounts` 权限检查失败而中断。

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
GitHub Actions
  ├─ build_dataset.py                # 收集、检测、排序 ProxyIP
  ├─ scripts/validate_outputs.py     # 校验 docs 输出一致性
  ├─ scripts/sync_kv.py              # 通过 Cloudflare REST API 写入 KV
  ├─ scripts/sync_dns.py             # 通过 Cloudflare REST API 同步 DNS-only A 记录
  ├─ wrangler deploy                 # 部署 Worker
  └─ scripts/audit.py                # 运行结果审计

Cloudflare
  ├─ Worker: cf-proxyip-us           # 分发页面、API、认证与缓存
  ├─ KV Namespace                    # 保存 current/all/full/top5 等数据
  └─ DNS-only A: proxyip.leilaomi.cc.cd -> 当前主 IP
```

关键点：

- Worker 只负责分发、认证、读取 KV、返回 API，不负责在线扫描 IP。
- 候选收集、检测、排序、failover 在 GitHub Actions 里完成。
- DNS 始终只写入 1 条 DNS-only A 记录，避免出口频繁跳变。
- KV 同步使用 Cloudflare REST API，不再使用 `wrangler kv key put`。
- Worker 部署仍使用 `wrangler deploy`。

## 数据产物

仓库 `docs/` 保留每次自动巡检后的可审计快照。

| 文件 | 说明 |
|---|---|
| `docs/current.txt` | 当前稳定主 IP，供 `proxyip.leilaomi.cc.cd` 使用 |
| `docs/current.json` | 当前主 IP 的详细状态 |
| `docs/state.json` | failover 状态、连续失败次数、最近成功时间 |
| `docs/history.json` | 主 IP 切换历史 |
| `docs/standby.txt` | 备用候选池 |
| `docs/top5.txt` | 当前主 IP + 前 4 个备用候选 |
| `docs/all.txt` | 通过 IPv4 与目标地区检测的全部候选 |
| `docs/us.txt` | 当前目标地区 US 列表 |
| `docs/best.txt` | 排名前 20 的候选 |
| `docs/dns-records.json` | 期望同步到 Cloudflare 的 DNS A 记录 |
| `docs/full.json` | 完整公开报告，不提交完整 debug `all_results` |
| `docs/kv-manifest.json` | KV key 与本地文件映射 |
| `docs/v2ray.txt` | Base64 编码 IP 列表 |

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

执行流程：

1. Checkout 仓库完整历史。
2. 安装 Node 20。
3. 安装 `wrangler@4`。
4. 语法检查：
   - `python3 -m py_compile build_dataset.py scripts/*.py`
   - `node --check worker.js`
5. 生成、校验和测试输出：
   - `python3 build_dataset.py`
   - `python3 scripts/validate_outputs.py`
   - `python3 -m unittest discover -s tests`
6. 执行完整自动更新：
   - `python3 scripts/auto_update.py`
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

只生成本地数据，不同步 Cloudflare：

```bash
python3 build_dataset.py
python3 scripts/validate_outputs.py
python3 -m unittest discover -s tests
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

常用环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROXYIP_TARGET_COUNTRIES` | `US` | 目标出口国家，可用逗号分隔 |
| `PROXYIP_PREFERRED_COLOS` | `IAD` | 同等风险下优先的 Cloudflare colo |
| `PROXYIP_RECORD_NAME` | `proxyip.leilaomi.cc.cd` | DNS-only A 记录名 |
| `PROXYIP_CURRENT_MIN_BOT_SCORE` | `80` | 当前主 IP 最低 bot score 门槛 |
| `PROXYIP_CURRENT_MAX_LATENCY_MS` | `2500` | 当前主 IP 最大延迟门槛 |
| `PROXYIP_SWITCH_COOLDOWN_HOURS` | `6` | 切换冷却时间，单位小时 |

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

## 最近一次 CI 修复说明

本仓库 Action 失败曾卡在 `Run ProxyIP auto update` 步骤。数据生成、测试和输出校验都正常，失败点是 KV 同步命令：

```bash
wrangler kv key put ... --namespace-id ... --remote
```

Wrangler 在 CI 中额外访问 Cloudflare `/memberships`、`/accounts` 时返回认证错误。

已完成的实际修复：

1. 更新 GitHub Secret `CLOUDFLARE_API_TOKEN`。
2. `scripts/sync_kv.py` 改为 Cloudflare REST API 写 KV。
3. workflow 增加 `CLOUDFLARE_ACCOUNT_ID` 环境变量。
4. 手动触发 `ProxyIP Auto Update` 验证通过。

相关提交：

- `40633b91fd1a4d17285cd8f0d7ae4eb22b80681e`：`fix: sync KV via Cloudflare REST API`
- `4e05b876d682ea24b6ebb4bdcf6ce9c929b91fc0`：`ci: pass Cloudflare account id to ProxyIP update`

验证通过的 workflow run：

```text
https://github.com/LeilaoMi/cf-proxyip-us/actions/runs/27439555741
```

## 延伸文档

- [docs/audit-2026-06-04.md](docs/audit-2026-06-04.md)：项目改进建议报告。
- [docs/implementation-plan-2026-06-04.md](docs/implementation-plan-2026-06-04.md)：分阶段落地计划。
- [docs/continue-2026-06-04.md](docs/continue-2026-06-04.md)：历史延续记录。