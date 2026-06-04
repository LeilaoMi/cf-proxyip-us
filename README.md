# ProxyIP US IPv4

Cloudflare Worker + DNS-only ProxyIP 项目：`list.leilaomi.cc.cd` 分发数据；`proxyip.leilaomi.cc.cd` 只解析到 1 个低风险且稳定的主 ProxyIP。当前主 IP 仍有效时不切换，连续失效后才 failover。

## 线上地址

- ProxyIP 域名（DNS-only 单 A 记录）：`proxyip.leilaomi.cc.cd`
- Worker 入口页：https://list.leilaomi.cc.cd/
- 当前主 ProxyIP：https://list.leilaomi.cc.cd/current.txt
- 当前主 ProxyIP 详情：https://list.leilaomi.cc.cd/current.json
- 备用候选：https://list.leilaomi.cc.cd/standby.txt
- 推荐 Top 5（当前 + 备用，不直接全量写入 DNS）：https://list.leilaomi.cc.cd/top5.txt
- 全量列表：https://list.leilaomi.cc.cd/all.txt
- US 列表：https://list.leilaomi.cc.cd/us.txt
- Top 20：https://list.leilaomi.cc.cd/best.txt
- 完整报告：https://list.leilaomi.cc.cd/full.json
- V2Ray Base64：https://list.leilaomi.cc.cd/v2ray.txt
- HMAC Token：https://list.leilaomi.cc.cd/token
- 健康检查：https://list.leilaomi.cc.cd/health
- 统计数据：https://list.leilaomi.cc.cd/stats

## 认证方式

数据接口需要认证，支持两种方式：

### 1. Cookie 认证（浏览器）
访问首页自动设置 `proxyip_access=ok` cookie，有效期 8 小时。

### 2. HMAC Token 认证（程式化访问）
```bash
# 1. 先访问首页获取 cookie
curl -c cookies.txt https://list.leilaomi.cc.cd/

# 2. 用 cookie 访问 /token 获取 HMAC token
curl -b cookies.txt https://list.leilaomi.cc.cd/token
# 返回: {"token":"20260531-abc123...","date":"20260531","mode":"hmac"}

# 3. 用 HMAC token 访问数据接口（推荐 Header，避免 token 进入 URL/日志）
curl -H "Authorization: Bearer 20260531-abc123..." "https://list.leilaomi.cc.cd/all.txt"

# 兼容旧客户端：仍可使用 query token，但不推荐
curl "https://list.leilaomi.cc.cd/all.txt?t=20260531-abc123..."
```

Token 格式：`YYYYMMDD-HMAC-SHA256-Hex`，每天自动更新。Worker 端必须配置 `PROXYIP_SECRET`；除非显式设置 `ALLOW_LEGACY_DATE_TOKEN=1`，否则不再接受纯日期 legacy token。

### 3. ETag 缓存
所有数据接口支持 ETag/304 缓存，客户端可通过 `If-None-Match` 头部减少频宽消耗。

## 当前实际数据

当前数据以线上 `/health`、`docs/current.txt`、`docs/full.json` 为准。候选数、有效 IP 数、延迟与 Top 5 会随每次自动巡检变化，README 不再写死动态数字。

快速查看：

```bash
curl https://list.leilaomi.cc.cd/health
cat docs/current.txt
python3 - <<'PY'
import json
from pathlib import Path
full = json.loads(Path("docs/full.json").read_text())
print(full["summary"])
PY
```

输出文件在 `docs/`：

- `docs/current.txt`：1 个当前稳定主 IP，用于 `proxyip.leilaomi.cc.cd` DNS-only 单 A 记录
- `docs/current.json`：当前主 IP 详情与状态
- `docs/state.json`：failover 状态、连续失败次数、最近成功时间
- `docs/history.json`：切换历史
- `docs/standby.txt`：备用候选池
- `docs/top5.txt`：当前主 IP + 前 4 个备用候选
- `docs/all.txt`：通过 IPv4 与目标地区检测的 IP
- `docs/us.txt`：目前等同 `docs/all.txt`
- `docs/best.txt`：前 20 个
- `docs/dns-records.json`：Cloudflare DNS A 记录快照
- `docs/full.json`：公开检测报告（不包含完整 debug `all_results`）
- `docs/v2ray.txt`：Base64 编码的纯 IP 列表；不是完整 V2Ray/VLESS 节点订阅

### 候选源策略

本项目只收集「第三方中转 ProxyIP」候选，不再使用 Cloudflare 官方 IP 段、普通 CF 优选 IP 或明显偏 CDN 边缘的来源。原因是本项目的用途是让 Cloudflare 服务访问套 CF CDN 的目标站，而不是寻找 Cloudflare 自身边缘节点。

目前来源分三类：

| 类型 | 说明 |
|---|---|
| 文字型 ProxyIP 源 | 例如 `zip.cm.edu.kg/all.txt`，要求内容本身是 ProxyIP 候选 |
| 域名型 ProxyIP 源 | 例如 CMLiussss 分区 ProxyIP、社区分享 ProxyIP 域名，脚本会解析 A 记录后再检测 |
| 手动源 | `allowlist.txt` / `denylist.txt`，用于临时保留或排除候选 |

`allowlist.txt` / `denylist.txt` 每行一个候选，支持以下格式：

```text
1.2.3.4
1.2.3.4:443
1.2.3.4:443#US
```

脚本会下载 Cloudflare 官方 IPv4 段并排除命中的候选，避免误把 Cloudflare 自身 IP 写入 ProxyIP 池。


## 客户端使用示例

`proxyip.leilaomi.cc.cd` 是给客户端填写的稳定 ProxyIP 域名；它只指向当前主 IP，不等于 `top5.txt` 的全部备用列表。

常见用法：

```text
ProxyIP / proxyIP / proxy_ip: proxyip.leilaomi.cc.cd
端口: 443
是否代理: DNS-only / 灰云
```

如果你的 VLESS Worker 或面板有 `proxyIP`、`PROXYIP`、`ProxyIP` 字段，填入：

```text
proxyip.leilaomi.cc.cd
```

不要把 `list.leilaomi.cc.cd` 填到 ProxyIP 字段；`list.` 是数据分发接口，`proxyip.` 才是实际出口域名。

## Cloudflare 部署

本仓库按「分发域名 + ProxyIP DNS-only 域名」设计，具体域名应按用户自己的 Cloudflare zone 调整，不要直接照抄示例域名。

需要准备：

| 配置项 | 说明 | 当前示例 |
|---|---|---|
| Worker name | Cloudflare Worker 服务名，见 `wrangler.toml` 的 `name` | `cf-proxyip-us` |
| Worker 分发域名 | 绑定到 Worker Custom Domain，用于首页/API/订阅分发 | `list.<你的域名>` |
| ProxyIP DNS-only 域名 | 灰云 A 记录，只指向当前稳定主 IP | `proxyip.<你的域名>` |
| Cloudflare zone | 你的根域或托管 zone | `<你的域名>` |

推荐做法：

- 分发域名使用子域名，例如 `list.example.com`；
- ProxyIP 域名使用另一个子域名，例如 `proxyip.example.com`；
- 不要把 Worker 直接绑到根域，除非你明确要替换根域用途；
- `ProxyIP DNS-only 域名` 必须是 DNS-only / 灰云 A 记录，不能橙云代理；
- `workers.dev` 与 preview URL 可关闭，只保留自定义域名。

本项目脚本支持用环境变量覆盖域名配置：

```bash
export PROXYIP_ZONE_NAME="example.com"
export PROXYIP_RECORD_NAME="proxyip.example.com"
export PROXYIP_LIST_DOMAIN="https://list.example.com"
```

配置见 `wrangler.toml`。Worker Custom Domain 需要在 Cloudflare Workers Custom Domains 中绑定到你的分发域名；ProxyIP 域名则由 `scripts/sync_dns.py` 同步为单条 DNS-only A 记录。

## 反爬与风险控制

Worker 做了基础防护，目标是降低公开列表被爬取和被滥用的风险：

- Worker 代码提供 `robots.txt` 禁止抓取；若 Cloudflare 启用了 managed robots / AI Content Signals，线上 `/robots.txt` 可能由 Cloudflare 接管；
- 所有响应加 `X-Robots-Tag: noindex,nofollow,noarchive`；
- 常见 bot / crawler / curl / wget / python-requests / 扫描器 UA 直接 403；带合法 `Authorization: Bearer` 的程序化请求优先通过 token 校验，降低误伤；
- 文本与 JSON 数据接口需要认证（Cookie 或 HMAC Token）；公开 `/health` 只返回最小健康信息，详细 `/health/full` 与 `/stats` 需要认证；
- 接口使用 `private, max-age=300`，避免被公共缓存长期保存；
- 支持 ETag/304 缓存，减少不必要的数据传输。

安全性：HMAC-SHA256 签名，密钥存储在 Cloudflare Worker Secrets 中；`/token` 使用 `private, no-store`，避免被公共缓存保存。

这不是强安全认证；如果要更严格，下一步应改为固定私密 token 或 Cloudflare Access。

Rate Limiting：接口使用 `private, max-age=300`，避免被公共缓存长期保存；支持 ETag/304 缓存，减少不必要的数据传输。

安全 Headers：所有响应加 `X-Robots-Tag: noindex,nofollow,noarchive`；常见 bot / crawler / curl / wget / python-requests / 扫描器 UA 直接 403。

## 重新生成数据

```bash
python3 build_dataset.py
```

脚本会：

1. 从配置的数据源下载候选 IP（自动去重）
2. 按 `PROXYIP_TARGET_COUNTRIES`、IPv4、端口等条件过滤
3. 调用检测接口验证可用性
4. 只保留 `success=true` 且 `supports_ipv4=true` 的结果
5. 将 `direct_https` 备用验证标记为 `fallback_unverified` 并降权排序，避免替代主检测的真实风险评分
6. Top5 / standby 按 ASN 做分散选择，降低同一 ASN 集中风险
7. 重写 `result.json` 和 `docs/`

只重新生成本地数据不会更新 Cloudflare。若要同步 KV、DNS 并部署 Worker，使用完整自动流程：

```bash
python3 scripts/auto_update.py
```

## 自动巡检与自愈

端到端脚本：

```bash
python3 scripts/auto_update.py
```

它会自动：

1. 重新生成数据；
2. 执行 `scripts/validate_outputs.py` 检查输出文件一致性；
3. 检测当前主 IP；健康且达到质量门槛时保持不变；连续失效或质量低于门槛后才 failover；
4. 同步数据到 Worker KV；
5. 同步 `PROXYIP_RECORD_NAME` 指定的 1 条 DNS-only A 记录；
6. 部署 Worker；
7. 验证 `PROXYIP_LIST_DOMAIN`、接口防护、DNS、HMAC Token；
8. 若数据有变化，只提交白名单数据产物，push 前先 `git pull --rebase --autostash origin main`。

自动化不依赖 Zo Computer；可在 GitHub Actions 托管 runner 定时执行。需要在 GitHub repo secrets 中保存 Cloudflare token 与 HMAC token secret，并按你的域名配置环境变量。

## GitHub Actions

自动化入口：`.github/workflows/proxyip-auto-update.yml`。

- Cron：每 3 小时一次，`17 */3 * * *` UTC。
- 可手动执行：GitHub repo → Actions → ProxyIP Auto Update → Run workflow。
- 使用 GitHub 内置 `GITHUB_TOKEN` 推送数据快照，checkout 使用完整历史以支持 rebase。
- 使用 repo secret `CLOUDFLARE_API_TOKEN` 更新 DNS 与部署 Worker。
- 使用 repo secret `PROXYIP_HMAC_SECRET` 生成 HMAC Token。
- 部署前先执行 `python3 -m py_compile build_dataset.py scripts/*.py`、`node --check worker.js`、`python3 scripts/validate_outputs.py`。

### 所需 Secrets
| Secret | 用途 |
|--------|------|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API 权限（DNS + Workers） |
| `PROXYIP_HMAC_SECRET` | GitHub Actions 用于生成 HMAC Token |

Cloudflare Worker 端还需要设置 Worker Secret：`PROXYIP_SECRET`。它的值必须与 GitHub repo secret `PROXYIP_HMAC_SECRET` 相同。若未配置，数据接口不再回退到纯日期 token。

## GitHub Pages

GitHub Pages 不是本项目的主要发布方式。实际发布由两部分组成：

- Worker Custom Domain：用于分发首页、API 和订阅内容；
- DNS-only A 记录：用于指向当前稳定 ProxyIP。

仓库保留 `docs/` 作为可审核的部署数据快照。

## 单一地区策略

目前策略是稳定优先，且默认锁定单一出口地区。具体地区由环境变量控制：

- `PROXYIP_TARGET_COUNTRIES`：目标出口国家，默认 `US`，可设为如 `JP`、`SG` 或逗号分隔列表；
- `PROXYIP_PREFERRED_COLOS`：同等风险下优先的 Cloudflare colo，默认 `IAD`；
- `PROXYIP_RECORD_NAME`：永远只同步 1 条 DNS-only A 记录；
- `PROXYIP_CURRENT_MIN_BOT_SCORE`：当前主 IP 的最低 bot score 门槛，默认 `80`；
- `PROXYIP_CURRENT_MAX_LATENCY_MS`：当前主 IP 的最大延迟门槛，默认 `2500`；
- `PROXYIP_SWITCH_COOLDOWN_HOURS`：切换冷却时间，默认 `6` 小时。

当前 IP 健康、符合目标地区且达到质量门槛时不切换，避免 AI / CDN 站点看到出口乱跳。如需改成其他地区，调整上述环境变量后重新跑 `scripts/auto_update.py`。


## 📖 延伸阅读

- [docs/audit-2026-06-04.md](docs/audit-2026-06-04.md) — 项目改进建议报告（2026-06-04，58 条）
- [docs/implementation-plan-2026-06-04.md](docs/implementation-plan-2026-06-04.md) — 分阶段落地计划


### 2026-06-02 — 稳定性与线上核查后改进

| 文件 | 改进 |
|------|------|
| `build_dataset.py` | 修复直接 HTTPS fallback 结果与 `enrich()` 字段不兼容的问题 |
| `build_dataset.py` | 增加当前主 IP 最低质量门槛与切换冷却时间 |
| `build_dataset.py` | `docs/full.json` 不再提交完整 `all_results` debug 数据，降低仓库体积 |
| `worker.js` | `/health` 改为最小公开信息，新增需认证的 `/health/full` |
| `worker.js` | `/stats` 改为需 Cookie/HMAC Token 认证 |
| `worker.js` | 增加数据新鲜度 stale 判断、HEAD 支持、304 安全 headers、CSP、Permissions-Policy |
| `worker.js` | Rate limiter 增加过期 bucket 清理 |
| `scripts/*.py` | 域名与 zone 支持环境变量覆盖 |
| `README.md` | 移除易过期的动态数字，明确 GitHub Secret 与 Worker Secret 的关系 |
