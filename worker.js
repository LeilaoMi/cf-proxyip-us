const EMPTY_RESULT = {
  summary: { current_ip: null, checked_at: null, cmliu_ipv4_valid: 0, total_candidates: 0 },
  current: null,
  state: { current_ip: null, status: "no_data", failure_count: 0 },
  history: [],
  standby: [],
  recommended_top5: [],
  valid_ips: [],
};

const ACCESS_COOKIE = "proxyip_access";
const ACCESS_VALUE = "ok";
const ACCESS_TTL = 60 * 60 * 8;
const STALE_WARN_MS = 6 * 60 * 60 * 1000;
const STALE_FAIL_MS = 12 * 60 * 60 * 1000;
const RATE_CLEANUP_EVERY = 100;
const RATE_MAX_BUCKETS = 5000;
const RESULT_CACHE_TTL_MS = 60_000;
const TEXT_KEY_BY_PATH = {
  "/current.txt": "current_txt",
  "/standby.txt": "standby_txt",
  "/all.txt": "all_txt",
  "/us.txt": "us_txt",
  "/best.txt": "best_txt",
  "/top5.txt": "top5_txt",
  "/v2ray.txt": "v2ray_txt",
  "/base64.txt": "base64_txt",
};
let rateRequestCount = 0;
let cachedResult = null;
let cachedResultAt = 0;

// ── Rate limiter (per-isolate, best-effort) ──
const RATE_WINDOW_MS = 60_000;  // 1 minute window
const RATE_MAX_REQUESTS = 60;   // max requests per window per IP
const rateBuckets = new Map();  // ip -> { count, windowStart }

function checkRateLimit(request) {
  const ip = request.headers.get("cf-connecting-ip") || request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  const now = Date.now();
  rateRequestCount++;
  if (rateRequestCount % RATE_CLEANUP_EVERY === 0 || rateBuckets.size > RATE_MAX_BUCKETS) {
    cleanupRateBuckets(now);
  }
  let bucket = rateBuckets.get(ip);
  if (!bucket || now - bucket.windowStart > RATE_WINDOW_MS) {
    bucket = { count: 0, windowStart: now };
    rateBuckets.set(ip, bucket);
  }
  bucket.count++;
  if (bucket.count > RATE_MAX_REQUESTS) {
    return { ok: false, ip, remaining: 0 };
  }
  return { ok: true, ip, remaining: RATE_MAX_REQUESTS - bucket.count };
}

function cleanupRateBuckets(now = Date.now()) {
  for (const [ip, bucket] of rateBuckets) {
    if (now - bucket.windowStart > RATE_WINDOW_MS * 2) rateBuckets.delete(ip);
  }
  if (rateBuckets.size <= RATE_MAX_BUCKETS) return;
  for (const ip of rateBuckets.keys()) {
    rateBuckets.delete(ip);
    if (rateBuckets.size <= RATE_MAX_BUCKETS) break;
  }
}

const TEXT_PATHS = new Set(["/current.txt", "/standby.txt", "/all.txt", "/us.txt", "/best.txt", "/top5.txt", "/v2ray.txt", "/base64.txt"]);
const JSON_PATHS = new Set(["/current.json", "/state.json", "/history.json", "/full.json"]);
const BLOCKED_UA = /(bot|spider|crawler|scrapy|python-requests|aiohttp|curl|wget|go-http-client|httpx|masscan|zgrab|nuclei|semrush|ahrefs|bytespider|petalbot|yandex|bingbot|googlebot)/i;

export default {
  async fetch(request, env) {
    const isHead = request.method === "HEAD";
    const reply = (resp) => isHead ? new Response(null, { status: resp.status, statusText: resp.statusText, headers: resp.headers }) : resp;

    // Rate limit check
    const rl = checkRateLimit(request);
    if (!rl.ok) {
      return reply(withHeaders(new Response("Rate limit exceeded\n", { status: 429, headers: { "retry-after": "60", "content-type": "text/plain" } }), true));
    }

    const url = new URL(request.url);

    if (request.method === "OPTIONS") return withHeaders(new Response(null, { status: 204 }));
    if (request.method !== "GET" && request.method !== "HEAD") return withHeaders(new Response("Method not allowed", { status: 405 }));
    if (url.pathname === "/robots.txt") return reply(text("User-agent: *\nDisallow: /\n", false));

    // /health is a minimal public health check. Detailed health requires auth.
    if (url.pathname === "/health") {
      const result = await loadResult(env);
      const valid = Array.isArray(result.valid_ips) ? result.valid_ips : [];
      const freshness = freshnessStatus(result.summary?.checked_at || null);
      return reply(json({
        ok: freshness.ok,
        stale: freshness.stale,
        age_minutes: freshness.ageMinutes,
        count: valid.length,
        checked_at: result.summary?.checked_at || null,
        data_source: env.PROXYIP_KV ? "kv" : "empty_fallback",
      }, true, true));
    }

    if (url.pathname === "/health/full") {
      const authErr = await verifyAccess(request, url, env);
      if (authErr) return reply(deny(authErr));
      const result = await loadResult(env);
      const valid = Array.isArray(result.valid_ips) ? result.valid_ips : [];
      const freshness = freshnessStatus(result.summary?.checked_at || null);
      return reply(json({
        ok: freshness.ok,
        stale: freshness.stale,
        age_minutes: freshness.ageMinutes,
        current: currentIp(result),
        standby_count: standby(result).length,
        count: valid.length,
        checked_at: result.summary?.checked_at || null,
        data_source: env.PROXYIP_KV ? "kv" : "empty_fallback",
      }, true));
    }

    // /stats returns detailed statistics (authenticated)
    if (url.pathname === "/stats") {
      const authErr = await verifyAccess(request, url, env);
      if (authErr) return reply(deny(authErr));
      const result = await loadResult(env);
      const valid = Array.isArray(result.valid_ips) ? result.valid_ips : [];
      return reply(json({
        ok: true,
        data: {
          asn_distribution: valid.map(v => v.risk?.asn).filter(Boolean).reduce((acc, asn) => {
            acc[asn] = (acc[asn] || 0) + 1;
            return acc;
          }, {}),
          colo_distribution: valid.map(v => v.risk?.colo).filter(Boolean).reduce((acc, colo) => {
            acc[colo] = (acc[colo] || 0) + 1;
            return acc;
          }, {}),
          freshness: result.summary?.checked_at || null,
          avg_latency: valid.reduce((sum, v) => sum + (v.latency_ms ?? 0), 0) / valid.length || null,
          latency_distribution: valid.map(v => v.latency_ms ?? null).filter(v => v !== null),
        },
      }, true));
    }

    // /token returns the HMAC token for programmatic access (cookie-authenticated)
    if (url.pathname === "/token") {
      const authErr = await verifyAccess(request, url, env);
      if (authErr) return reply(deny(authErr));
      const secret = env.PROXYIP_SECRET || "";
      const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      if (secret) {
        const hex = await hmacHex(secret, today);
        return reply(noStoreJson({ token: `${today}-${hex}`, date: today, mode: "hmac" }));
      }
      if (env.ALLOW_LEGACY_DATE_TOKEN === "1") return reply(noStoreJson({ token: today, date: today, mode: "legacy" }));
      return reply(withHeaders(new Response("PROXYIP_SECRET is not configured\n", { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } }), true));
    }

    // Auth gate for data endpoints
    if (TEXT_PATHS.has(url.pathname) || JSON_PATHS.has(url.pathname)) {
      const authErr = await verifyAccess(request, url, env);
      if (authErr) return reply(deny(authErr));
    }

    const lightweightText = await loadTextKey(env, url.pathname);
    if (lightweightText !== null) {
      const cacheSeconds = url.pathname === "/current.txt" ? 60 : 300;
      return reply(withEtag(text(lightweightText, true, false, cacheSeconds), etagForText(url.pathname, lightweightText)));
    }

    const result = await loadResult(env);
    const valid = Array.isArray(result.valid_ips) ? result.valid_ips : [];
    const ips = valid.map((item) => item.ip).filter(Boolean);
    const etag = resultEtag(result);

    // 304 Not Modified
    if (request.headers.get("if-none-match") === etag) {
      return reply(withHeaders(new Response(null, { status: 304, headers: { etag } }), true));
    }

    if (url.pathname === "/current.txt") return reply(withEtag(text(lines(currentIp(result) ? [currentIp(result)] : []), true, false, 60), etag));
    if (url.pathname === "/standby.txt") return reply(withEtag(text(lines(standby(result).map((item) => item.ip).filter(Boolean)), true), etag));
    if (url.pathname === "/all.txt" || url.pathname === "/us.txt") return reply(withEtag(text(lines(ips), true), etag));
    if (url.pathname === "/best.txt") {
      const n = Math.min(Math.max(Number.parseInt(url.searchParams.get("n") || "20", 10) || 20, 1), 100);
      return reply(withEtag(text(lines(ips.slice(0, n)), true), etag));
    }
    if (url.pathname === "/top5.txt") return reply(withEtag(text(lines(top5(result)), true), etag));
    if (url.pathname === "/v2ray.txt" || url.pathname === "/base64.txt") return reply(withEtag(text(btoa(ips.join("\n")), true), etag));
    if (url.pathname === "/current.json") return reply(withEtag(json({ current: result.current || null, state: result.state || null }, true), etag));
    if (url.pathname === "/state.json") return reply(withEtag(json(result.state || {}, true), etag));
    if (url.pathname === "/history.json") return reply(withEtag(json(result.history || [], true), etag));
    if (url.pathname === "/full.json") return reply(withEtag(json(result, true), etag));

    return reply(html(renderHome(result, url)));
  }
};

// ── Data loading (KV only) ──

async function loadResult(env) {
  const now = Date.now();
  if (cachedResult && now - cachedResultAt < RESULT_CACHE_TTL_MS) return cachedResult;
  if (env.PROXYIP_KV) {
    const stored = await env.PROXYIP_KV.get("result_json", "json");
    if (stored) {
      cachedResult = stored;
      cachedResultAt = now;
      return stored;
    }
  }
  return EMPTY_RESULT;
}

async function loadTextKey(env, pathname) {
  if (!env.PROXYIP_KV) return null;
  const key = TEXT_KEY_BY_PATH[pathname];
  if (!key) return null;
  const value = await env.PROXYIP_KV.get(key, "text");
  return typeof value === "string" ? value : null;
}

// ── Access control ──

async function verifyAccess(request, url, env) {
  const token = bearerToken(request) || url.searchParams.get("t") || "";
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const secret = env.PROXYIP_SECRET || "";

  if (secret) {
    if (token.startsWith(today + "-")) {
      const expected = await hmacHex(secret, today);
      if (token.slice(today.length + 1) === expected) return null;
    }
  } else if (env.ALLOW_LEGACY_DATE_TOKEN === "1" && token === today) {
    return null;
  }

  const ua = request.headers.get("user-agent") || "";
  if (BLOCKED_UA.test(ua)) return "blocked user-agent";

  // Cookie from homepage visit
  if ((request.headers.get("cookie") || "").includes(`${ACCESS_COOKIE}=${ACCESS_VALUE}`)) return null;

  if (!secret) return "server token secret is not configured";
  return "invalid or missing token";
}

function bearerToken(request) {
  const auth = request.headers.get("authorization") || "";
  return auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
}

// ── HMAC key cache ──

const hmacKeyCache = new Map();  // secret -> CryptoKey (per-isolate, no expiry needed since secret is stable)
const hmacResultCache = new Map();  // "secret|message" -> { hex, expiresAt }
const HMAC_CACHE_TTL_MS = 60_000;  // 60s cache for same day's HMAC

async function hmacHex(secret, message) {
  const cacheKey = `${secret}|${message}`;
  const cached = hmacResultCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) return cached.hex;

  let key = hmacKeyCache.get(secret);
  if (!key) {
    key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    hmacKeyCache.set(secret, key);
  }
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  const hex = Array.from(new Uint8Array(sig)).map((b) => b.toString(16).padStart(2, "0")).join("");
  hmacResultCache.set(cacheKey, { hex, expiresAt: Date.now() + HMAC_CACHE_TTL_MS });
  return hex;
}

// ── Helpers ──

function currentIp(result) { return result.current?.ip || result.state?.current_ip || null; }
function standby(result) { return Array.isArray(result.standby) ? result.standby : []; }
function top5(result) { return (Array.isArray(result.recommended_top5) ? result.recommended_top5 : []).map((i) => i.ip).filter(Boolean); }
function lines(items) { return items.join("\n") + (items.length ? "\n" : ""); }
function resultEtag(result) {
  return `"${result.summary?.checked_at || "0"}:${currentIp(result) || "none"}:${Array.isArray(result.valid_ips) ? result.valid_ips.length : 0}"`;
}
function etagForText(pathname, body) {
  return `"${pathname}:${body.length}:${hashString(body)}"`;
}
function hashString(value) {
  let hash = 0;
  for (let i = 0; i < value.length; i++) hash = ((hash << 5) - hash + value.charCodeAt(i)) | 0;
  return Math.abs(hash).toString(36);
}
function withEtag(resp, etag) {
  const h = new Headers(resp.headers);
  h.set("etag", etag);
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: h });
}
function freshnessStatus(checkedAt) {
  if (!checkedAt) return { ok: false, stale: true, ageMinutes: null };
  const ts = new Date(checkedAt).getTime();
  if (!Number.isFinite(ts)) return { ok: false, stale: true, ageMinutes: null };
  const ageMs = Date.now() - ts;
  return {
    ok: ageMs <= STALE_FAIL_MS,
    stale: ageMs > STALE_WARN_MS,
    ageMinutes: Math.max(0, Math.round(ageMs / 60000)),
  };
}

// ── Response builders ──

function deny(reason) {
  console.log(`Access denied: ${reason}`);
  return withHeaders(new Response("Forbidden\n", { status: 403, headers: { "content-type": "text/plain; charset=utf-8" } }), false);
}
function text(body, privateCache, allowCors = false, maxAge = 300) {
  return withHeaders(new Response(body, { headers: { "content-type": "text/plain; charset=utf-8" } }), privateCache, allowCors, maxAge);
}
function json(data, privateCache, allowCors = false, maxAge = 300) {
  return withHeaders(new Response(JSON.stringify(data, null, 2), { headers: { "content-type": "application/json; charset=utf-8" } }), privateCache, allowCors, maxAge);
}
function noStoreJson(data) {
  const response = withHeaders(new Response(JSON.stringify(data, null, 2), { headers: { "content-type": "application/json; charset=utf-8" } }), true);
  response.headers.set("cache-control", "private, no-store");
  return response;
}
function html(body) {
  return withHeaders(new Response(body, { headers: { "content-type": "text/html; charset=utf-8", "set-cookie": `${ACCESS_COOKIE}=${ACCESS_VALUE}; Max-Age=${ACCESS_TTL}; Path=/; Secure; HttpOnly; SameSite=Lax` } }), false);
}
function withHeaders(response, privateCache = false, allowCors = false, maxAge = 300) {
  const headers = new Headers(response.headers);
  if (allowCors) headers.set("access-control-allow-origin", "*");
  headers.set("cache-control", `${privateCache ? "private" : "public"}, max-age=${maxAge}`);
  headers.set("x-robots-tag", "noindex, nofollow, noarchive");
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "no-referrer");
  headers.set("strict-transport-security", "max-age=31536000; includeSubDomains");
  headers.set("x-frame-options", "DENY");
  headers.set("content-security-policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
  headers.set("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

// ── Homepage ──

function renderHome(result, url) {
  const s = result.summary || {};
  const valid = Array.isArray(result.valid_ips) ? result.valid_ips : [];
  const current = currentIp(result);
  const standbyCount = standby(result).length;
  const top5Ips = top5(result);
  const checkedAt = s.checked_at || "unknown";
  const freshnessMin = checkedAt !== "unknown" ? Math.round((Date.now() - new Date(checkedAt).getTime()) / 60000) : null;
  const asnSet = new Set(valid.map(i => i.risk?.asn).filter(Boolean));
  const sourceCount = s.source_count || s.sources?.length || 1;
  const avgLatency = valid.length > 0 ? Math.round(valid.reduce((sum, v) => sum + (v.latency_ms ?? 0), 0) / valid.length) : null;
  const rows = valid.slice(0, 30).map((item, i) =>
    `<tr><td>${i + 1}</td><td><code>${escapeHtml(item.ip)}</code></td><td>${escapeHtml(item.portRemote || 443)}</td><td>${escapeHtml(item.colo || "")}</td><td>${escapeHtml(item.latency_ms ?? "")}</td></tr>`
  ).join("");

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>ProxyIP US IPv4</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:960px;margin:40px auto;padding:0 20px;line-height:1.55;color:#111827;background:#fff}
h1{margin-bottom:4px}h2{margin-top:28px;margin-bottom:8px}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
code{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:0.9em}
.card{display:inline-block;border:1px solid #e5e7eb;border-radius:12px;padding:14px 18px;margin:6px 8px 6px 0;background:#fafafa}
.card b{display:block;font-size:24px}
.current-ip{font-size:20px;font-weight:600;color:#047857;background:#ecfdf5;display:inline-block;padding:8px 16px;border-radius:8px;border:1px solid #a7f3d0}
table{width:100%;border-collapse:collapse;margin-top:14px}td,th{border-bottom:1px solid #e5e7eb;padding:9px;text-align:left;font-size:14px}
th{font-weight:600;background:#f9fafb}
.muted{color:#6b7280;font-size:13px}.ok{color:#047857}
.endpoint-list{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px}
.endpoint-list li{margin:0}
.endpoint-list a{display:block;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#fafafa;font-family:monospace;font-size:13px}
.endpoint-list a:hover{background:#f0f9ff;border-color:#93c5fd}
#token-box{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 16px;margin:12px 0;font-family:monospace;font-size:13px;word-break:break-all;display:none;position:relative}
#token-box .copy-btn{position:absolute;top:8px;right:8px;background:#047857;color:#fff;border:none;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px}
#token-box .copy-btn:hover{background:#065f46}
.status-row{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}
.status-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:500}
.status-chip.ok{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0}
.status-chip.info{background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe}
</style>
</head>
<body>
<h1>ProxyIP US IPv4</h1>
<p class="muted">只收录 <code>zip.cm.edu.kg/all.txt</code> 中标记 <code>#US</code>、端口 <code>443</code>、且 cmliu 检测 <code>supports_ipv4=true</code> 的结果。</p>

<div class="status-row">
  <span class="status-chip ok">✓ 通过验证: <b>${escapeHtml(s.cmliu_ipv4_valid ?? valid.length)}</b></span>
  <span class="status-chip info">候选池: <b>${escapeHtml(s.total_candidates ?? valid.length)}</b></span>
  <span class="status-chip info">备用数: <b>${standbyCount}</b></span>
  <span class="status-chip info">ASN 分散: <b>${asnSet.size}</b></span>
  <span class="status-chip info">数据源: <b>${sourceCount}</b></span>
  <span class="status-chip info">平均延迟: <b>${avgLatency != null ? escapeHtml(avgLatency) : "N/A"}</b></span>
</div>
<p class="muted">上次更新: ${escapeHtml(checkedAt)}${freshnessMin !== null ? ` (${freshnessMin}m ago)` : ''}${freshnessMin !== null && freshnessMin > 240 ? ' ⚠️' : ''}</p>

<h2>当前稳定主 IP</h2>
<div class="current-ip">${escapeHtml(current || "none")}</div>

<h2>备用候选</h2>
<p>${top5Ips.map((ip, i) => `<code>${escapeHtml(ip)}</code>`).join(" &nbsp; ")}</p>

<h2>接口列表</h2>
<p class="muted">访问首页后 cookie 自动生效，点击即可查看数据。程序化访问请用 <a href="/token">/token</a> 获取 HMAC token。</p>
<ul class="endpoint-list">
  <li><a href="/current.txt">current.txt</a></li>
  <li><a href="/current.json">current.json</a></li>
  <li><a href="/standby.txt">standby.txt</a></li>
  <li><a href="/top5.txt">top5.txt</a></li>
  <li><a href="/all.txt">all.txt</a></li>
  <li><a href="/us.txt">us.txt</a></li>
  <li><a href="/best.txt">best.txt</a></li>
  <li><a href="/base64.txt">base64.txt</a></li>
  <li><a href="/history.json">history.json</a></li>
  <li><a href="/full.json">full.json</a></li>
  <li><a href="/token">🔑 /token</a></li>
  <li><a href="/health">💚 /health</a></li>
  <li><a href="/stats">📊 /stats（需 Cookie/Token）</a></li>
</ul>

<h2>API Token</h2>
<button onclick="fetchToken()" style="background:#2563eb;color:#fff;border:none;border-radius:8px;padding:10px 20px;cursor:pointer;font-size:14px">生成今日 HMAC Token</button>
<div id="token-box"><button class="copy-btn" onclick="copyToken()">复制</button><span id="token-value"></span></div>
<p class="muted">程序化用法：<code>curl -A "Mozilla/5.0" -H "Authorization: Bearer TOKEN" "https://list.leilaomi.cc.cd/current.txt"</code></p>

<h2>IP 列表（前 30）</h2>
<table>
<thead><tr><th>#</th><th>IP</th><th>Port</th><th>Colo</th><th>Latency</th></tr></thead>
<tbody>${rows}</tbody>
</table>

<p class="muted" style="margin-top:40px">由 Zo Computer 驱动</p>

<script>
async function fetchToken() {
  const btn = document.querySelector('button');
  const box = document.getElementById('token-box');
  const val = document.getElementById('token-value');
  
  btn.disabled = true;
  btn.textContent = '生成中...';
  box.style.display = 'none';
  
  try {
    const r = await fetch('/token');
    if (!r.ok) {
      const text = await r.text();
      throw new Error(text || 'HTTP ' + r.status);
    }
    const d = await r.json();
    if (!d.token) throw new Error('No token in response');
    val.textContent = d.token;
    box.style.display = 'block';
    btn.textContent = '✓ 已生成';
    setTimeout(() => btn.textContent = '生成今日 HMAC Token', 2000);
  } catch (e) { 
    alert('生成失败: ' + e.message + '\n\n请先访问首页获取 cookie');
    btn.textContent = '重试';
  } finally {
    btn.disabled = false;
  }
}

function copyToken() {
  const t = document.getElementById('token-value').textContent;
  if (!t) return;
  navigator.clipboard.writeText(t).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = '✓ Copied';
    setTimeout(() => btn.textContent = '复制', 1500);
  }).catch(() => {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = t;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    const btn = document.querySelector('.copy-btn');
    btn.textContent = '✓ Copied';
    setTimeout(() => btn.textContent = '复制', 1500);
  });
}
</script>
</body>
</html>`;
}
