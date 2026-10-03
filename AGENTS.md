# cf-proxyip-us 维护说明

## 项目定位

本项目用于维护一个 Cloudflare Worker + KV + DNS-only 单 A 记录的 ProxyIP 分发服务。

核心目标：

- 自动收集、检测、排序美国方向 ProxyIP 候选。
- 保持 1 个稳定当前主 IP。
- 当前主 IP 健康时不频繁切换。
- 当前主 IP 失效后从备用池 failover。
- 通过 Worker 分发 current、standby、top5、full、v2ray 等数据。
- 通过 Cloudflare DNS-only A 记录发布实际 ProxyIP 域名。

工作目录以当前检出的仓库为准（当前为 `C:\Users\leilaomi\Documents\Default Project\cf-proxyip-us`，Windows + PowerShell）。仓库外不存在需要同步维护的副本。

## 重要规则

- 未经明确确认，不要执行线上操作：
  - `wrangler deploy`
  - `scripts/sync_dns.py`
  - `scripts/sync_kv.py`
  - `scripts/auto_update.py`
  - `git push`
- 不要把 Cloudflare API Token、GitHub Token、HMAC Secret 写入仓库、日志、README 或记忆。
- README 示例中只能使用 `<Cloudflare API Token>`、`<HMAC Secret>` 等占位符。
- `docs/full.json`、`docs/ip_history.json`、`docs/state.json` 是数据产物，由脚本生成；改动只应来自脚本，不要手改。
- `docs/probe_local.json` 由本机 `PROXYIP_PROBE_ONLY=1` 运行负责提交，CI 的 `git add` 白名单里没有它，不要把它加进白名单覆盖。
- `docs/full.slim.json` 和根目录 `result.json` 是中间产物，已 gitignore，不要提交。

## 排序管线（四阶段）

`build_dataset.py` 每次运行按四阶段产出排序，细节见 README「排序与测速：四个阶段」：

1. 本地 RTT 与抖动：本机 `PROXYIP_PROBE_SAMPLES` 次 TCP+TLS 采样，P50 作 `latency_ms`，max-min 作 `rtt_jitter_ms`。
2. 滚动稳定性历史：读 `docs/ip_history.json` 的 7 天成功率与 `PROXYIP_HISTORY_LAT_WINDOW` 次历史均值。
3. 两级筛选与真实吞吐：廉价 RTT 筛出 `PROXYIP_THROUGHPUT_TOP_N` + 当前主 IP，经 ProxyIP 下载 10MB 测 MB/s。
4. 本地探测叠加：有 `docs/probe_local.json` 且未过期时按权重并入前三项。

先过质量门槛（bot score、地区、P50、抖动、有效样本数、7 天成功率、`stability.gate_ok`），再按有效延迟升序。吞吐只进有效延迟与当前主 IP 门槛，不进质量门槛。

## 当前实际同步方式

- KV 同步：`scripts/sync_kv.py` 通过 Cloudflare REST API 写入 KV。
- DNS 同步：`scripts/sync_dns.py` 通过 Cloudflare REST API 更新 DNS-only A 记录。
- Worker 部署：`wrangler deploy`。
- GitHub Actions：`.github/workflows/proxyip-auto-update.yml`，cron `17 */3 * * *` UTC，job 超时 45 分钟，job env 带 `PROXYIP_SKIP_GENERATE=1`。

KV 同步不再使用 `wrangler kv key put`，避免 CI 中 Wrangler 额外请求 `/memberships`、`/accounts` 时因 token 权限组合导致认证失败。排障见 README「排障指南」。

## 关键文件

| 文件 | 作用 |
|---|---|
| `build_dataset.py` | 候选收集、验证、四阶段探测排序、failover、生成 `docs/` 输出 |
| `worker.js` | Worker 路由、认证、API、KV 读取、公开状态页 |
| `scripts/auto_update.py` | CI 端到端自动流程：同步、部署、线上验证、审计、提交 |
| `scripts/sync_kv.py` | 使用 Cloudflare REST API 同步 KV |
| `scripts/sync_dns.py` | 使用 Cloudflare REST API 同步 DNS-only A 记录 |
| `scripts/validate_outputs.py` | 本地输出一致性校验 |
| `scripts/audit.py` | 部署后审计 |
| `tests/test_build_dataset.py` | 单元测试：探测、门槛、稳定性历史、吞吐、本地探测、输出形状 |
| `.github/workflows/proxyip-auto-update.yml` | GitHub Actions 定时入口 |
| `wrangler.toml` | Worker、KV binding、Cloudflare 部署配置 |
| `docs/kv-manifest.json` | KV key 与本地文件映射 |
| `docs/operations.md` | 运行与部署说明、所需权限、排障 |
| `docs/archive/` | 2026-06 的审计与阶段计划，已落地归档，不代表当前行为 |
| `README.md` | 面向使用者的完整说明 |
| `AGENTS.md` | 本文件 |

## 修改后必须验证

代码或配置变更后，至少运行（Windows 上 `python3` 若不可用则用 `python`）：

```bash
python3 -m py_compile build_dataset.py scripts/*.py
node --check worker.js
python3 scripts/validate_outputs.py
python3 -m unittest discover -s tests
```

PowerShell 不会展开 `scripts/*.py`，需要手动展开：

```powershell
$files = @("build_dataset.py") + (Get-ChildItem scripts\*.py | ForEach-Object { $_.FullName })
python -m py_compile @files
```

如果变更涉及 CI、KV、DNS 或 Worker 部署，需要手动触发 GitHub Actions 验证：

```bash
gh workflow run proxyip-auto-update.yml -R LeilaoMi/cf-proxyip-us --ref main
```

然后查看 run 是否 success：

```bash
gh run list -R LeilaoMi/cf-proxyip-us --limit 5
gh run watch <run-id> -R LeilaoMi/cf-proxyip-us --interval 15 --exit-status
```

## 必需环境变量 / Secrets

GitHub Actions 需要：

- `CLOUDFLARE_ACCOUNT_ID`：Cloudflare Account ID，workflow env 明文传入即可。
- `CLOUDFLARE_API_TOKEN`：GitHub Secret，不得明文提交。
- `PROXYIP_HMAC_SECRET`：GitHub Secret，不得明文提交。
- `GITHUB_TOKEN`：GitHub Actions 内置。

Cloudflare Worker 需要：

- `PROXYIP_SECRET`：Worker Secret，值应与 `PROXYIP_HMAC_SECRET` 一致。

本地跑完整流程还需要 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`PROXYIP_HMAC_SECRET`；默认没有本地 wrangler 登录态，KV/DNS 走 REST API 不依赖 wrangler。

全部可调环境变量见 README「环境变量」。

## 线上现状

- live：https://proxyip.leilaomi.cc.cd
- status：https://proxyip.leilaomi.cc.cd/status
- current：https://proxyip.leilaomi.cc.cd/current.txt
- KV：13 个 key，含 `current`、`standby`、`top5`、`full`、`ip_history`、`state`
- 当前主 IP、候选数量、延迟、吞吐以 `current.txt` 和 `status` 为准，不要写死在文档里
