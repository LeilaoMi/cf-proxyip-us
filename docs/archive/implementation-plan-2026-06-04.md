# cf-proxyip-us 阶段推进计划（2026-06-04）

来源：`docs/audit-2026-06-04.md`。

## 总原则

- 先安全边界，再自动化可靠性，再数据质量，最后体验和长期架构。
- 每阶段只做可验证的小改动；通过本地验证后再进入下一阶段。
- 不在未确认时执行线上部署、DNS 修改、KV 写入、GitHub push。

## 阶段 1：安全与 CI 基线（当前阶段）

目标：先挡住最明显的安全/自动化风险。

落地项：

1. 禁止 `PROXYIP_SECRET` 缺失时继续接受日期 token。
2. `/token` 改为 `private, no-store`。
3. 新增 `Authorization: Bearer <token>` 支持，保留 query token 兼容。
4. 新增纯本地 `scripts/validate_outputs.py`。
5. workflow 增加语法检查与输出一致性验证。
6. `scripts/auto_update.py` 改为只提交白名单文件，push 前 rebase。

验证：

```bash
python3 -m py_compile build_dataset.py scripts/*.py
node --check worker.js
python3 scripts/validate_outputs.py
```

## 阶段 2：访问控制收紧

目标：减少 token 泄漏和跨域读取风险。

落地项：

1. 数据接口收紧 CORS，仅公开健康检查允许 `*`。
2. 对外 403 统一文案，详细原因只写日志。
3. README 示例改为 Header Bearer，query token 仅标注兼容。
4. 合法 Bearer token 请求跳过 UA 黑名单，降低误伤。

验证：

```bash
node --check worker.js
curl -I https://list.leilaomi.cc.cd/health
curl -I https://list.leilaomi.cc.cd/all.txt
```

线上 curl 只在部署后执行。

## 阶段 3：数据质量与排序

目标：让 fallback 与 Top5 更可信、更分散。

落地项：

1. `direct_https` fallback 标记为低可信，不按真实高分排序。
2. Top5 同 ASN 最多 1 个，standby 同 ASN 最多 2 个。
3. 区分 `candidate_colo` 与 `exit_colo`。
4. README 补充 `allowlist.txt` / `denylist.txt` 格式。

验证：

```bash
python3 build_dataset.py
python3 scripts/validate_outputs.py
```

## 阶段 4：Worker 性能与 KV 策略

目标：降低 Worker 每次请求读大 JSON 与重复计算。

落地项：

1. Worker 增加 30-60 秒 isolate 内存缓存。
2. 文本接口优先读 KV 轻量 key。
3. `/current.txt` 缓存降到 60 秒。
4. ETag 使用 `checked_at + current_ip + valid_count`。

验证：

```bash
node --check worker.js
```

部署后再看 `/health/full` 与各文本接口。

## 阶段 5：测试与文档

目标：降低后续修改风险。

落地项：

1. 新增基础测试目录，覆盖输出一致性、选择逻辑、鉴权 token。
2. README 逐步统一简体中文。
3. 补充客户端 ProxyIP 使用示例。
4. 标记 canonical path：`Projects/cf-proxyip-us`。

验证：

```bash
python3 -m py_compile build_dataset.py scripts/*.py
python3 scripts/validate_outputs.py
```

## 阶段 6：长期仓库健康

目标：控制长期自动提交带来的仓库膨胀。

候选方案：

1. `main` 只放代码，数据产物放 `data` 分支。
2. 或使用 GitHub Release / artifact 保存历史快照。
3. 引入 `ip_history.json` 累积 7 天质量趋势。

此阶段改动较大，必须单独确认后执行。
