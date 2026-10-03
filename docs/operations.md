# 运行与部署说明

本文档记录 `cf-proxyip-us` 当前真实运行方式，方便后续排障和维护。

## 1. 自动化流程

GitHub Actions workflow：

```text
.github/workflows/proxyip-auto-update.yml
```

触发方式：

- 定时：`17 */3 * * *`，每 3 小时一次。
- 手动：Actions 页面执行 `ProxyIP Auto Update`。

主要步骤：

1. Checkout 仓库完整历史。
2. 安装 Node 20。
3. 安装 `wrangler@4`。
4. Python / Worker 语法检查。
5. `build_dataset.py` 生成 ProxyIP 数据（四阶段探测排序，见 README「排序与测速：四个阶段」）。
6. 校验 `docs/` 输出。
7. 运行单元测试。
8. 执行 `scripts/auto_update.py`（job env 带 `PROXYIP_SKIP_GENERATE=1`，跳过第 5 步已做过的生成）。
9. 同步 KV。
10. 同步 DNS-only A 记录。
11. 部署 Worker。
12. 线上验证。
13. 如数据变化，按白名单提交 `docs/` 快照。

job 超时 45 分钟。workflow 写 `node-version: "20"`，GitHub 托管 runner 已弃用 Node 20，可能被强制升级，只要 `wrangler@4` 装得上即可。

手动触发与跟踪：

```bash
gh workflow run proxyip-auto-update.yml -R LeilaoMi/cf-proxyip-us --ref main
gh run list -R LeilaoMi/cf-proxyip-us --limit 5
gh run watch <run-id> -R LeilaoMi/cf-proxyip-us --interval 15 --exit-status
```

## 2. KV 同步

当前 KV 同步脚本：

```text
scripts/sync_kv.py
```

同步方式：Cloudflare REST API。

接口格式：

```text
PUT /client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}
```

配置来源：

| 配置 | 来源 |
|---|---|
| Account ID | `CLOUDFLARE_ACCOUNT_ID` |
| API Token | `CLOUDFLARE_API_TOKEN` |
| KV Namespace ID | `wrangler.toml` |
| KV key 列表 | `docs/kv-manifest.json` |

`docs/kv-manifest.json` 里 `result_json` 指向 `docs/full.json`，但上传前脚本会把内容读出来、再跑一遍 `slim_full()`（剔除 `all_results`）写到 `docs/full.slim.json`，然后 PUT 这个瘦身版。由于 `docs/full.json` 生成时已经剔过 `all_results` 和 `throughput_by_ip`，实际差异主要是重新按 `indent=2` 序列化。`docs/full.slim.json` 已 gitignore，不要提交。

不要再改回 `wrangler kv key put`，否则可能再次触发 Wrangler 在 CI 中访问 `/memberships`、`/accounts` 导致认证失败。

## 3. DNS 同步

当前 DNS 同步脚本：

```text
scripts/sync_dns.py
```

默认同步目标：

```text
proxyip.leilaomi.cc.cd
```

要求：

- 只保留 1 条 A 记录。
- 必须是 DNS-only / 灰云。
- 内容来自 `docs/dns-records.json`。

## 4. Worker 部署

Worker 部署仍使用：

```bash
wrangler deploy
```

因此 Cloudflare API Token 仍需要具备 Workers Scripts 编辑权限。

## 5. 必需权限

Cloudflare API Token 至少需要：

- Account / Workers Scripts：编辑。
- Account / Workers KV Storage：编辑。
- Zone / DNS：编辑。
- Zone / Zone：读取。

实际权限名称以 Cloudflare 控制台为准。

## 6. 常见失败处理

### KV 同步失败

检查：

- `CLOUDFLARE_ACCOUNT_ID` 是否传入。
- `CLOUDFLARE_API_TOKEN` Secret 是否存在。
- Token 是否具备 Workers KV Storage 编辑权限。
- `wrangler.toml` 中 KV namespace ID 是否正确。

### DNS 同步失败

检查：

- Token 是否具备 Zone DNS 编辑权限。
- `PROXYIP_ZONE_NAME` 是否正确。
- `PROXYIP_RECORD_NAME` 是否属于该 Zone。

### Worker 部署失败

检查：

- Token 是否具备 Workers Scripts 编辑权限。
- `wrangler.toml` 中 Worker name、KV binding、route 是否正确。
- `wrangler@4` 是否安装成功。

### 线上 Token 认证失败

检查：

- GitHub Secret `PROXYIP_HMAC_SECRET` 是否存在。
- Cloudflare Worker Secret `PROXYIP_SECRET` 是否存在。
- 两者是否一致。

## 7. 验证方式

看最新一次运行，不要依赖固定的 run 编号：

- https://github.com/LeilaoMi/cf-proxyip-us/actions

一次 success 的运行意味着：

- 数据生成成功。
- 输出校验成功。
- 单元测试成功。
- KV 同步成功。
- DNS 同步成功。
- Worker 部署成功。
- 线上 `current.txt` 与 `docs/` 一致。

部署后也可直接看线上状态：

```text
https://proxyip.leilaomi.cc.cd/status
https://proxyip.leilaomi.cc.cd/current.txt
```
