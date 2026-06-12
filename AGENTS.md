# cf-proxyip-us 维护说明

Canonical path：`/home/workspace/Projects/cf-proxyip-us`。

## 项目定位

本项目用于维护一个 Cloudflare Worker + KV + DNS-only 单 A 记录的 ProxyIP 分发服务。

核心目标：

- 自动收集、检测、排序美国方向 ProxyIP 候选。
- 保持 1 个稳定当前主 IP。
- 当前主 IP 健康时不频繁切换。
- 当前主 IP 失效后从备用池 failover。
- 通过 Worker 分发 current、standby、top5、full、v2ray 等数据。
- 通过 Cloudflare DNS-only A 记录发布实际 ProxyIP 域名。

## 重要规则

- 不要编辑 `/home/workspace/cf-proxyip-us`，那是旧重复副本。
- 未经明确确认，不要执行线上操作：
  - `wrangler deploy`
  - `scripts/sync_dns.py`
  - `scripts/sync_kv.py`
  - `scripts/auto_update.py`
  - `git push`
- 不要把 Cloudflare API Token、GitHub Token、HMAC Secret 写入仓库、日志、README 或记忆。
- README 示例中只能使用 `<Cloudflare API Token>`、`<HMAC Secret>` 等占位符。

## 当前实际同步方式

- KV 同步：`scripts/sync_kv.py` 通过 Cloudflare REST API 写入 KV。
- DNS 同步：`scripts/sync_dns.py` 通过 Cloudflare REST API 更新 DNS-only A 记录。
- Worker 部署：`wrangler deploy`。
- GitHub Actions：`.github/workflows/proxyip-auto-update.yml`。

KV 同步不再使用 `wrangler kv key put`，避免 CI 中 Wrangler 额外请求 `/memberships`、`/accounts` 时因 token 权限组合导致认证失败。

## 关键文件

| 文件 | 作用 |
|---|---|
| `build_dataset.py` | 候选收集、验证、排序、failover、生成 `docs/` 输出 |
| `worker.js` | Worker 路由、认证、API、KV 读取、公开状态页 |
| `scripts/auto_update.py` | CI 端到端自动流程：生成、校验、同步、部署、审计、提交 |
| `scripts/sync_kv.py` | 使用 Cloudflare REST API 同步 KV |
| `scripts/sync_dns.py` | 使用 Cloudflare REST API 同步 DNS-only A 记录 |
| `scripts/validate_outputs.py` | 本地输出一致性校验 |
| `.github/workflows/proxyip-auto-update.yml` | GitHub Actions 定时入口 |
| `wrangler.toml` | Worker、KV binding、Cloudflare 部署配置 |
| `docs/kv-manifest.json` | KV key 与本地文件映射 |

## 修改后必须验证

代码或配置变更后，至少运行：

```bash
python3 -m py_compile build_dataset.py scripts/*.py
node --check worker.js
python3 scripts/validate_outputs.py
python3 -m unittest discover -s tests
```

如果变更涉及 CI、KV、DNS 或 Worker 部署，需要手动触发 GitHub Actions 验证：

```bash
gh workflow run proxyip-auto-update.yml -R LeilaoMi/cf-proxyip-us --ref main
```

然后查看 run 是否 success。

## 必需环境变量 / Secrets

GitHub Actions 需要：

- `CLOUDFLARE_ACCOUNT_ID`：Cloudflare Account ID，workflow env 明文传入即可。
- `CLOUDFLARE_API_TOKEN`：GitHub Secret，不得明文提交。
- `PROXYIP_HMAC_SECRET`：GitHub Secret，不得明文提交。
- `GITHUB_TOKEN`：GitHub Actions 内置。

Cloudflare Worker 需要：

- `PROXYIP_SECRET`：Worker Secret，值应与 `PROXYIP_HMAC_SECRET` 一致。

## 当前已完成的 CI 修复

此前 Action 失败点在 `Run ProxyIP auto update`，具体是 `scripts/sync_kv.py` 使用 `wrangler kv key put` 时 Wrangler 认证失败。

已修复为：

- `scripts/sync_kv.py` 改用 Cloudflare REST API。
- workflow 传入 `CLOUDFLARE_ACCOUNT_ID`。
- GitHub Secret `CLOUDFLARE_API_TOKEN` 已更新。
- 手动验证 run `27439555741` 已成功。