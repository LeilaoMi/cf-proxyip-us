# ProxyIP US IPv4

Cloudflare Worker 分發服務：只發布 **US / IPv4 / 443** 且通過 cmliu 檢測、明確 `supports_ipv4=true` 的 ProxyIP 候選。

## 線上地址

- 入口頁：https://proxyip.leilaomi.cc.cd/
- 全量列表：https://proxyip.leilaomi.cc.cd/all.txt?t=YYYYMMDD
- US 列表：https://proxyip.leilaomi.cc.cd/us.txt?t=YYYYMMDD
- Top 20：https://proxyip.leilaomi.cc.cd/best.txt?t=YYYYMMDD
- 完整報告：https://proxyip.leilaomi.cc.cd/full.json?t=YYYYMMDD
- V2Ray Base64：https://proxyip.leilaomi.cc.cd/v2ray.txt?t=YYYYMMDD

`YYYYMMDD` 是當天 UTC 日期，例如 `20260527`。也可以先打開入口頁取得 cookie，再直接訪問接口。

## 當前實際數據

- 來源：`file https://zip.cm.edu.kg/all.txt`
- 過濾：只取 `#US`、只取 IPv4、只取 `:443`
- cmliu 條件：`success=true` 且 `supports_ipv4=true`
- 候選數：573
- 通過 IPv4 檢測：210
- cmliu 成功但不是 IPv4：352（已排除）
- 檢測接口：`https://api.090227.xyz/check`
- 最近生成時間：`2026-05-27T12:06:14.786080+00:00`
- 線上驗證：2026-05-27 20:10（Asia/Shanghai）已確認入口、token、cookie gate、bot block、workers.dev 關閉、GitHub Pages 關閉、前 10 個樣本重新通過 cmliu IPv4 檢測。

輸出文件在 `docs/`：

- `file docs/all.txt`：210 個通過 IPv4 檢測的 IP
- `file docs/us.txt`：同 `file all.txt`
- `file docs/best.txt`：前 20 個
- `file docs/full.json`：檢測報告
- `file docs/v2ray.txt`：Base64 編碼列表

## Cloudflare 部署

- Worker name：`cf-proxyip-us`
- 自定義域名：`proxyip.leilaomi.cc.cd`
- 主域 `leilaomi.cc.cd` **不綁定**此 Worker；實測不命中本 Worker
- `workers.dev` 與 preview URL 已在配置中關閉，不作為對外地址；實測 `cf-proxyip-us.horjane.workers.dev` 返回 404/1042

配置見 `file wrangler.toml`。自定義域名在 Cloudflare Workers Custom Domains 中已綁定到 `proxyip.leilaomi.cc.cd`。

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

重新生成後，需把 `file docs/full.json` 內嵌到 `file worker.js`，再部署：

```bash
wrangler deploy
```

## GitHub Pages

GitHub Pages 已關閉；實際發布只走 Cloudflare Worker。倉庫保留 `docs/` 作為可審核的部署數據快照。