# ProxyIP US IPv4

Cloudflare Worker + DNS-only ProxyIP 專案：`list.leilaomi.cc.cd` 分發資料；`proxyip.leilaomi.cc.cd` 只解析到 1 個低風險且穩定的主 ProxyIP。當前主 IP 仍有效時不切換，連續失效後才 failover。

## 線上地址

- ProxyIP 域名（DNS-only 單 A 記錄）：`proxyip.leilaomi.cc.cd`
- Worker 入口頁：https://list.leilaomi.cc.cd/
- 當前主 ProxyIP：https://list.leilaomi.cc.cd/current.txt?t=YYYYMMDD
- 當前主 ProxyIP 詳情：https://list.leilaomi.cc.cd/current.json?t=YYYYMMDD
- 備用候選：https://list.leilaomi.cc.cd/standby.txt?t=YYYYMMDD
- 推薦 Top 5（當前 + 備用，不直接全量寫入 DNS）：https://list.leilaomi.cc.cd/top5.txt?t=YYYYMMDD
- 全量列表：https://list.leilaomi.cc.cd/all.txt?t=YYYYMMDD
- US 列表：https://list.leilaomi.cc.cd/us.txt?t=YYYYMMDD
- Top 20：https://list.leilaomi.cc.cd/best.txt?t=YYYYMMDD
- 完整報告：https://list.leilaomi.cc.cd/full.json?t=YYYYMMDD
- V2Ray Base64：https://list.leilaomi.cc.cd/v2ray.txt?t=YYYYMMDD

`YYYYMMDD` 是當天 UTC 日期，例如 `20260527`。也可以先打開入口頁取得 cookie，再直接訪問接口。

## 當前實際數據

- 來源：`file https://zip.cm.edu.kg/all.txt`
- 過濾：只取 `#US`、只取 IPv4、只取 `:443`
- cmliu 條件：`success=true` 且 `supports_ipv4=true`
- 候選數：573
- 通過 IPv4 檢測：210
- cmliu 成功但不是 IPv4：352（已排除）
- 低風險 Top 5：`128.14.196.39`, `162.243.115.21`, `8.221.126.227`, `43.170.25.96`, `150.136.105.229`
- 排名規則：Cloudflare bot score 高、`corporateProxy=false`、`verifiedBot=false`、延遲低；Top 5 保持 ASN 分散
- 檢測接口：`https://api.090227.xyz/check`
- 最近生成時間：`2026-05-27T13:31:23.566470+00:00`
- 線上驗證：2026-05-27 21:21（Asia/Shanghai）已確認 `list.leilaomi.cc.cd` 入口、token、bot block、workers.dev 關閉、`proxyip.leilaomi.cc.cd` DNS-only Top 5 A 記錄、Top 5 全部重新通過 cmliu IPv4 檢測。

輸出文件在 `docs/`：

- `file docs/current.txt`：1 個當前穩定主 IP，用於 `proxyip.leilaomi.cc.cd` DNS-only 單 A 記錄
- `file docs/current.json`：當前主 IP 詳情與狀態
- `file docs/state.json`：failover 狀態、連續失敗次數、最近成功時間
- `file docs/history.json`：切換歷史
- `file docs/standby.txt`：備用候選池
- `file docs/top5.txt`：當前主 IP + 前 4 個備用候選
- `file docs/all.txt`：207 個通過 IPv4 檢測的 IP
- `file docs/us.txt`：同 `file all.txt`
- `file docs/best.txt`：前 20 個
- `file docs/dns-records.json`：Cloudflare DNS A 記錄快照
- `file docs/full.json`：檢測報告
- `file docs/v2ray.txt`：Base64 編碼列表

## Cloudflare 部署

- Worker name：`cf-proxyip-us`
- Worker 自定義域名：`list.leilaomi.cc.cd`
- ProxyIP DNS-only 域名：`proxyip.leilaomi.cc.cd`，1 條 A 記錄、灰雲、不經 Cloudflare 代理
- 主域 `leilaomi.cc.cd` **不綁定**此 Worker；實測不命中本 Worker
- `workers.dev` 與 preview URL 已在配置中關閉，不作為對外地址；實測 `cf-proxyip-us.horjane.workers.dev` 返回 404/1042

配置見 `file wrangler.toml`。Worker 自定義域名在 Cloudflare Workers Custom Domains 中綁定到 `list.leilaomi.cc.cd`；`proxyip.leilaomi.cc.cd` 是 DNS-only A 記錄。

## 反爬與風險控制

Worker 做了基礎防護，目標是降低公開列表被爬取和被濫用的風險：

- `file robots.txt` 禁止抓取；
- 所有響應加 `X-Robots-Tag: noindex,nofollow,noarchive`；
- 常見 bot / crawler / curl / wget / python-requests / 掃描器 UA 直接 403；
- 文本與 JSON 接口需要：
  - 先訪問首頁取得 cookie；或
  - 帶當天 UTC token：`?t=YYYYMMDD`；
- 接口使用 `private, max-age=300`，避免被公共緩存長期保存。

這不是強安全認證；如果要更嚴格，下一步應改為固定私密 token 或 Cloudflare Access。

## 重新生成數據

```bash
python3 build_dataset.py
```

腳本會：

1. 下載 `file https://zip.cm.edu.kg/all.txt`
2. 只保留 `#US`、IPv4、`:443`
3. 調用 `https://api.090227.xyz/check?proxyip=<ip>` 驗證
4. 只保留 `success=true` 且 `supports_ipv4=true`
5. 重寫 `file result.json` 和 `docs/`

重新生成後，需把精簡後的 `file docs/full.json` 內嵌到 `file worker.js`，更新 Cloudflare DNS A 記錄，再部署：

```bash
wrangler deploy
```

## 自動巡檢與自癒

已提供端到端腳本：

```bash
python3 scripts/auto_update.py
```

它會自動：

1. 重新生成數據；
2. 檢測當前主 IP；仍有效則保持不變；連續失效後才從候選池 failover；
3. 把精簡數據內嵌到 Worker；
4. 同步 `proxyip.leilaomi.cc.cd` 的 1 條 DNS-only A 記錄；
5. 部署 Worker；
6. 驗證 `list.leilaomi.cc.cd`、接口防護、DNS Top 5；
7. 若數據有變化，自動 commit 並 push 到 GitHub。

自動化不依賴 Zo Computer；已遷移到 GitHub Actions，每 3 小時在 GitHub 託管 runner 執行一次，避免 Zo 休眠導致漏跑。需要在 GitHub repo secrets 中保存 `CLOUDFLARE_API_TOKEN`。

## GitHub Actions

自動化入口：`file .github/workflows/proxyip-auto-update.yml`。

- Cron：每 3 小時一次，`17 */3 * * *` UTC。
- 可手動執行：GitHub repo → Actions → ProxyIP Auto Update → Run workflow。
- 使用 GitHub 內建 `GITHUB_TOKEN` 推送資料快照。
- 使用 repo secret `CLOUDFLARE_API_TOKEN` 更新 DNS 與部署 Worker。

## GitHub Pages

GitHub Pages 已關閉；實際發布為 `proxyip.leilaomi.cc.cd` 的 DNS-only A 記錄與 `list.leilaomi.cc.cd` 的 Cloudflare Worker。倉庫保留 `docs/` 作為可審核的部署數據快照。

## 單一地區策略

目前策略是穩定優先，且鎖定單一出口地區：

- `PROXYIP_TARGET_COUNTRIES=US`：只保留檢測出口國家為 US 的 ProxyIP；
- `PROXYIP_PREFERRED_COLOS=IAD`：同等風險下優先 IAD / Ashburn；
- `proxyip.leilaomi.cc.cd` 永遠只同步 1 條 DNS-only A 記錄；
- 當前 IP 健康且仍符合目標地區時不切換，避免 AI / CF CDN 站點看到出口亂跳；
- 如需改成其他單一地區，調整 GitHub Actions/環境變量中的 `PROXYIP_TARGET_COUNTRIES` 和 `PROXYIP_PREFERRED_COLOS` 後重新跑 `scripts/auto_update.py`。
