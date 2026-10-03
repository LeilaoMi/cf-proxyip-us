#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import ssl
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = [
    {"name": "zip.cm.edu.kg/all.txt", "url": "https://zip.cm.edu.kg/all.txt", "countries": {"US"}, "ports": {443}},
]

PROXYIP_DOMAIN_SOURCES = [
    {
        "name": "cmliussss-region-proxyip",
        "domains": [
            "ProxyIP.US.CMLiussss.net",
            "ProxyIP.SG.CMLiussss.net",
            "ProxyIP.JP.CMLiussss.net",
            "ProxyIP.HK.CMLiussss.net",
            "ProxyIP.KR.CMLiussss.net",
            "ProxyIP.DE.CMLiussss.net",
            "ProxyIP.SE.CMLiussss.net",
            "ProxyIP.NL.CMLiussss.net",
            "ProxyIP.FI.CMLiussss.net",
            "ProxyIP.GB.CMLiussss.net",
        ],
    },
    {
        "name": "community-proxyip-domains",
        "domains": [
            "edgetunnel.anycast.eu.org",
            "ts.hpc.tw",
            "cdn.xn--b6gac.eu.org",
            "cdn-all.xn--b6gac.eu.org",
            "bestproxy.onecf.eu.org",
        ],
    },
]

CLOUDFLARE_IPS_V4_URL = "https://www.cloudflare.com/ips-v4"
CHECK_API = "https://api.090227.xyz/check"
USER_AGENT = "cf-proxyip-stable-builder/2.0"
MAX_WORKERS = int(os.environ.get("PROXYIP_MAX_WORKERS", "24"))
TIMEOUT = int(os.environ.get("PROXYIP_CHECK_TIMEOUT", "35"))
MAX_CANDIDATES = int(os.environ.get("PROXYIP_MAX_CANDIDATES", "1400"))
FAILOVER_THRESHOLD = int(os.environ.get("PROXYIP_FAILOVER_THRESHOLD", "2"))
FALLBACK_SOURCES: list[dict] = []
CURRENT_MIN_BOT_SCORE = int(os.environ.get("PROXYIP_CURRENT_MIN_BOT_SCORE", "80"))
CURRENT_MAX_LATENCY_MS = int(os.environ.get("PROXYIP_CURRENT_MAX_LATENCY_MS", "2500"))
SWITCH_COOLDOWN_HOURS = int(os.environ.get("PROXYIP_SWITCH_COOLDOWN_HOURS", "6"))
TARGET_COUNTRIES = {x.strip().upper() for x in os.environ.get("PROXYIP_TARGET_COUNTRIES", "US").split(",") if x.strip()}
PREFERRED_COLOS = [x.strip().upper() for x in os.environ.get("PROXYIP_PREFERRED_COLOS", "IAD").split(",") if x.strip()]
BEST_COUNT = 20
STANDBY_COUNT = 10

# Stage 1: local multi-sample TCP+TLS handshake probe.
PROBE_SAMPLES = int(os.environ.get("PROXYIP_PROBE_SAMPLES", "3"))
PROBE_TIMEOUT = int(os.environ.get("PROXYIP_PROBE_TIMEOUT", "5"))
PROBE_MAX_CONSECUTIVE_FAILURES = 2
PROBE_SNI = "speed.cloudflare.com"
# 采样不足时 jitter 会被算成 0，等于把测量失败伪装成稳定，必须单独判负。
PROBE_MIN_SAMPLES = int(os.environ.get("PROXYIP_PROBE_MIN_SAMPLES", str(max(1, (PROBE_SAMPLES + 1) // 2))))
PROBE_SAMPLE_DEFICIT_PENALTY_MS = int(os.environ.get("PROXYIP_PROBE_SAMPLE_DEFICIT_PENALTY_MS", "2000"))
MAX_JITTER_MS = int(os.environ.get("PROXYIP_MAX_JITTER_MS", "500"))
JITTER_WEIGHT = int(os.environ.get("PROXYIP_JITTER_WEIGHT", "2"))

# Stage 2: rolling stability history feeding ranking and current-IP quality.
HISTORY_WINDOW = int(os.environ.get("PROXYIP_HISTORY_WINDOW", "56"))
HISTORY_LAT_WINDOW = int(os.environ.get("PROXYIP_HISTORY_LAT_WINDOW", "8"))
HISTORY_RETENTION_DAYS = int(os.environ.get("PROXYIP_HISTORY_RETENTION_DAYS", "7"))
MIN_HISTORY_CHECKS = int(os.environ.get("PROXYIP_MIN_HISTORY_CHECKS", "6"))
MIN_SUCCESS_RATE_7D = float(os.environ.get("PROXYIP_MIN_SUCCESS_RATE_7D", "0.9"))
HISTORY_UNKNOWN_RATE = float(os.environ.get("PROXYIP_HISTORY_UNKNOWN_RATE", "0.85"))
STABILITY_PENALTY_MS = int(os.environ.get("PROXYIP_STABILITY_PENALTY_MS", "4000"))
LATENCY_BLEND_WEIGHT = 0.5

IP_RE = re.compile(r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?::(?P<port>\d{1,5}))?(?:#(?P<country>[A-Z]{2}))?")
DOCS = Path("docs")
STATE_PATH = DOCS / "state.json"
CURRENT_PATH = DOCS / "current.txt"
HISTORY_PATH = DOCS / "history.json"
IP_HISTORY_PATH = DOCS / "ip_history.json"
MANUAL_ALLOWLIST = Path("allowlist.txt")
MANUAL_DENYLIST = Path("denylist.txt")

# Stage 3: cheap RTT screen to a small pool, then real download throughput on that pool.
THROUGHPUT_TOP_N = int(os.environ.get("PROXYIP_THROUGHPUT_TOP_N", "30"))
THROUGHPUT_BYTES = int(os.environ.get("PROXYIP_THROUGHPUT_BYTES", str(10 * 1024 * 1024)))
THROUGHPUT_TIMEOUT = int(os.environ.get("PROXYIP_THROUGHPUT_TIMEOUT", "12"))
THROUGHPUT_WORKERS = int(os.environ.get("PROXYIP_THROUGHPUT_WORKERS", "1"))
THROUGHPUT_ABORT_S = float(os.environ.get("PROXYIP_THROUGHPUT_ABORT_S", "4"))
THROUGHPUT_ABORT_MBPS = float(os.environ.get("PROXYIP_THROUGHPUT_ABORT_MBPS", "0.4"))
# 传 10MB 的真实耗时折算进有效延迟；未测速时按 THROUGHPUT_UNKNOWN_MS 计，排到测过速的后面。
THROUGHPUT_WEIGHT = float(os.environ.get("PROXYIP_THROUGHPUT_WEIGHT", "1.0"))
THROUGHPUT_UNKNOWN_MS = float(os.environ.get("PROXYIP_THROUGHPUT_UNKNOWN_MS", "20000"))
THROUGHPUT_MIN_MBPS = float(os.environ.get("PROXYIP_THROUGHPUT_MIN_MBPS", "1.0"))
THROUGHPUT_MAX_AGE_HOURS = float(os.environ.get("PROXYIP_THROUGHPUT_MAX_AGE_HOURS", "72"))
THROUGHPUT_CI_WEIGHT = float(os.environ.get("PROXYIP_THROUGHPUT_CI_WEIGHT", "0.4"))

# Stage 4: local probe overlay. No VPS, so the operator's own machine publishes
# docs/probe_local.json and CI merges it as weighted data.
PROBE_ONLY = os.environ.get("PROXYIP_PROBE_ONLY", "") == "1"
LOCAL_PROBE_PATH = DOCS / "probe_local.json"
LOCAL_PROBE_WEIGHT = float(os.environ.get("PROXYIP_LOCAL_PROBE_WEIGHT", "0.6"))
LOCAL_PROBE_MAX_AGE_HOURS = float(os.environ.get("PROXYIP_LOCAL_PROBE_MAX_AGE_HOURS", "168"))
LOCAL_THROUGHPUT_WEIGHT = float(os.environ.get("PROXYIP_LOCAL_THROUGHPUT_WEIGHT", "0.6"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_text(url: str, retries: int = 3) -> str:
    last_err = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=45) as res:
                return res.read().decode("utf-8", "ignore")
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise last_err


def valid_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
        return addr.is_global
    except ValueError:
        return False


def load_cloudflare_ipv4_networks() -> list[ipaddress.IPv4Network]:
    try:
        text = fetch_text(CLOUDFLARE_IPS_V4_URL, retries=2)
        return [ipaddress.IPv4Network(line.strip()) for line in text.splitlines() if line.strip()]
    except Exception as exc:
        print(f"warning: failed to load Cloudflare IPv4 ranges: {exc}")
        return []


def is_cloudflare_official_ip(ip: str, networks: list[ipaddress.IPv4Network]) -> bool:
    if not networks:
        return False
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def resolve_domain(domain: str) -> list[str]:
    try:
        rows = socket.getaddrinfo(domain, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({row[4][0] for row in rows if valid_ipv4(row[4][0])})


def parse_candidates(text: str, source_name: str, countries: set[str] | None = None, ports: set[int] | None = None) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = IP_RE.search(line)
        if not m:
            continue
        ip = m.group("ip")
        if not valid_ipv4(ip):
            continue
        port = int(m.group("port") or 443)
        country = m.group("country")
        if countries and country not in countries:
            continue
        if ports and port not in ports:
            continue
        out.append({"ip": ip, "port": port, "country_hint": country, "sources": [source_name]})
    return out


def read_ip_file(path: Path, source_name: str) -> list[dict]:
    if not path.exists():
        return []
    return parse_candidates(path.read_text(encoding="utf-8", errors="ignore"), source_name)


def add_candidate(by_ip: dict[str, dict], row: dict, cf_networks: list[ipaddress.IPv4Network]) -> bool:
    ip = row["ip"]
    if is_cloudflare_official_ip(ip, cf_networks):
        return False
    item = by_ip.setdefault(ip, {
        "ip": ip,
        "port": row.get("port", 443),
        "country_hint": row.get("country_hint"),
        "sources": [],
        "source_domains": [],
    })
    item["sources"] = sorted(set(item["sources"]) | set(row.get("sources") or []))
    if row.get("source_domain"):
        item["source_domains"] = sorted(set(item.get("source_domains") or []) | {row["source_domain"]})
    if row.get("source_type"):
        item["source_type"] = row["source_type"]
    return True


def collect_candidates() -> tuple[list[dict], list[dict]]:
    by_ip: dict[str, dict] = {}
    source_stats: list[dict] = []
    cf_networks = load_cloudflare_ipv4_networks()

    for src in SOURCES + FALLBACK_SOURCES:
        skipped_cf = 0
        try:
            text = fetch_text(src["url"])
            rows = parse_candidates(text, src["name"], src.get("countries"), src.get("ports"))
            for row in rows:
                if not add_candidate(by_ip, row, cf_networks):
                    skipped_cf += 1
            source_stats.append({
                "name": src["name"],
                "type": "text_proxyip",
                "url": src["url"],
                "count": len(rows),
                "accepted": len(rows) - skipped_cf,
                "skipped_cloudflare_official": skipped_cf,
                "error": None,
            })
        except Exception as exc:
            source_stats.append({"name": src["name"], "type": "text_proxyip", "url": src["url"], "count": 0, "accepted": 0, "skipped_cloudflare_official": skipped_cf, "error": str(exc)})

    for src in PROXYIP_DOMAIN_SOURCES:
        resolved = 0
        accepted = 0
        skipped_cf = 0
        domain_errors: dict[str, str] = {}
        for domain in src["domains"]:
            ips = resolve_domain(domain)
            if not ips:
                domain_errors[domain] = "no A record"
                continue
            resolved += len(ips)
            for ip in ips:
                row = {
                    "ip": ip,
                    "port": 443,
                    "country_hint": None,
                    "sources": [src["name"]],
                    "source_domain": domain,
                    "source_type": "domain_proxyip",
                }
                if add_candidate(by_ip, row, cf_networks):
                    accepted += 1
                else:
                    skipped_cf += 1
        source_stats.append({
            "name": src["name"],
            "type": "domain_proxyip",
            "domains": src["domains"],
            "count": resolved,
            "accepted": accepted,
            "skipped_cloudflare_official": skipped_cf,
            "errors": domain_errors,
            "error": None if accepted or resolved else "no domains resolved",
        })

    for row in read_ip_file(MANUAL_ALLOWLIST, "manual_allowlist"):
        add_candidate(by_ip, row, cf_networks)

    current = read_current_ip()
    if current and valid_ipv4(current):
        add_candidate(by_ip, {"ip": current, "port": 443, "country_hint": None, "sources": ["current_dns"]}, cf_networks)

    deny = {x["ip"] for x in read_ip_file(MANUAL_DENYLIST, "manual_denylist")}
    candidates = [x for x in by_ip.values() if x["ip"] not in deny]
    candidates.sort(key=lambda x: ("current_dns" not in x.get("sources", []), x["ip"]))
    return candidates[:MAX_CANDIDATES], source_stats

def check_cmliu(ip: str, retries: int = 2) -> dict:
    url = f"{CHECK_API}?proxyip={ip}"
    last_err = None
    for attempt in range(retries):
        start = time.monotonic()
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=TIMEOUT) as res:
                data = json.loads(res.read().decode("utf-8", "ignore"))
            data["latency_ms"] = int((time.monotonic() - start) * 1000)
            data["ip"] = ip
            return data
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(1)
    return {"ip": ip, "success": False, "error": str(last_err), "latency_ms": TIMEOUT * 1000}


def check_https_direct(ip: str, timeout: int = 8) -> dict:
    """直接 HTTPS 测试 ProxyIP，作为 cmliu API 的备用验证方式"""
    start = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        sock = socket.create_connection((ip, 443), timeout=timeout)
        ssock = ctx.wrap_socket(sock, server_hostname="speed.cloudflare.com")
        ssock.sendall(
            b"GET /cdn-cgi/trace HTTP/1.1\r\n"
            b"Host: speed.cloudflare.com\r\n"
            b"User-Agent: curl/8.0.0\r\n"
            b"Accept: */*\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )

        response = b""
        while True:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            response += chunk
        ssock.close()

        latency = int((time.monotonic() - start) * 1000)
        resp_text = response.decode("utf-8", errors="ignore")
        header_text, _, body_text = resp_text.partition("\r\n\r\n")
        headers_lower = header_text.lower()
        status_line = header_text.splitlines()[0] if header_text.splitlines() else ""
        is_cf = "cf-ray:" in headers_lower or "server: cloudflare" in headers_lower
        is_200 = " 200" in status_line

        country = None
        for line in body_text.splitlines():
            if line.startswith("loc="):
                country = line.split("=", 1)[1].strip().upper()
                break

        if is_cf and is_200:
            return {
                "ip": ip,
                "success": True,
                "supports_ipv4": True,
                "latency_ms": latency,
                "country": country or "US",
                "colo": None,
                "cf_bot_score": 95,
                "method": "direct_https",
                "fallback_unverified": True,
            }
        return {"ip": ip, "success": False, "error": "not_cloudflare", "latency_ms": latency}

    except Exception as exc:
        return {"ip": ip, "success": False, "error": str(exc)[:100], "latency_ms": int((time.monotonic() - start) * 1000)}

def check_with_fallback(ip: str) -> dict:
    """先用 cmliu API，失败则用直接 HTTPS 测试"""
    result = check_cmliu(ip)
    
    # 如果 cmliu API 成功，直接返回
    if result.get("success") is True and result.get("supports_ipv4") is True:
        return result
    
    # 如果 cmliu API 失败或超时，尝试直接 HTTPS 测试
    if result.get("success") is False and ("timeout" in str(result.get("error", "")).lower() or "urlopen" in str(result.get("error", "")).lower()):
        direct_result = check_https_direct(ip)
        if direct_result.get("success"):
            return direct_result

    return result


def tcp_tls_rtt(ip: str, timeout: int = PROBE_TIMEOUT) -> int:
    """本机到 ProxyIP 的 TCP connect + TLS 握手耗时（毫秒），不发 HTTP。"""
    start = time.monotonic()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = socket.create_connection((ip, 443), timeout=timeout)
    try:
        with ctx.wrap_socket(sock, server_hostname=PROBE_SNI):
            pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return int((time.monotonic() - start) * 1000)


def probe_rtt(ip: str, samples: int = PROBE_SAMPLES, timeout: int = PROBE_TIMEOUT) -> dict:
    """多次握手采样，返回 P50 延迟与抖动（max-min），不依赖第三方测速 API。"""
    measured: list[int] = []
    consecutive_failures = 0
    for _ in range(max(1, samples)):
        try:
            measured.append(tcp_tls_rtt(ip, timeout))
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= PROBE_MAX_CONSECUTIVE_FAILURES:
                break
    if not measured:
        return {"rtt_p50_ms": None, "rtt_jitter_ms": None, "rtt_samples": 0, "rtt_ok": False}
    measured.sort()
    return {
        "rtt_p50_ms": int(round(statistics.median(measured))),
        "rtt_jitter_ms": measured[-1] - measured[0],
        "rtt_samples": len(measured),
        "rtt_ok": True,
    }


def apply_probe(item: dict, probe: dict) -> dict:
    """把本地探测结果挂到候选上，latency_ms 改义为本机握手 P50。"""
    item["api_latency_ms"] = item.get("latency_ms")
    exit_probe = ((item.get("probe_results") or {}).get("ipv4") or {})
    if exit_probe.get("connect_ms") is not None:
        item["api_connect_ms"] = exit_probe.get("connect_ms")
    if exit_probe.get("tls_ms") is not None:
        item["api_tls_ms"] = exit_probe.get("tls_ms")
    item.update(probe)
    item["probe_attempted"] = True
    item["latency_ms"] = probe["rtt_p50_ms"] if probe.get("rtt_ok") else PROBE_TIMEOUT * 1000
    return item


def probe_throughput(ip: str, byte_budget: int = THROUGHPUT_BYTES, timeout: int = THROUGHPUT_TIMEOUT) -> dict:
    """经 ProxyIP 真实下载 speed.cloudflare.com/__down，记录 MB/s。

    两级筛选的第二级：握手快不等于传得快，主 IP 必须按吞吐定。
    计时从响应头之后的首个字节开始，只算 payload 传输，不含建连。
    """
    result = {
        "ok": False,
        "mbps": None,
        "bytes": 0,
        "duration_ms": 0,
        "ttfb_ms": 0,
        "complete": False,
        "error": None,
    }
    started = time.monotonic()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = None
    ssock = None
    try:
        sock = socket.create_connection((ip, 443), timeout=timeout)
        ssock = ctx.wrap_socket(sock, server_hostname=PROBE_SNI)
        ssock.settimeout(timeout)
        ssock.sendall(
            (
                f"GET /__down?bytes={byte_budget} HTTP/1.1\r\n"
                f"Host: {PROBE_SNI}\r\n"
                "User-Agent: curl/8.0.0\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()
        )

        buf = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in buf:
            if time.monotonic() > deadline:
                raise TimeoutError("header timeout")
            chunk = ssock.recv(65536)
            if not chunk:
                raise ConnectionError("closed before response headers")
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        if " 200" not in status_line:
            raise RuntimeError(f"unexpected status {status_line[:60]}")
        result["ttfb_ms"] = int((time.monotonic() - started) * 1000)

        received = len(body)
        began = time.monotonic()
        while received < byte_budget:
            elapsed = time.monotonic() - began
            if elapsed >= timeout:
                break
            if elapsed >= THROUGHPUT_ABORT_S and received / max(elapsed, 1e-6) < THROUGHPUT_ABORT_MBPS:
                break
            try:
                chunk = ssock.recv(65536)
            except (socket.timeout, TimeoutError, ssl.SSLError):
                break
            if not chunk:
                break
            received += len(chunk)
        duration = time.monotonic() - began

        if received <= 0 or duration <= 0:
            raise RuntimeError("no payload received")
        result.update({
            "ok": True,
            "mbps": round(received / 1_000_000 / duration, 3),
            "bytes": received,
            "duration_ms": int(duration * 1000),
            "complete": received >= byte_budget,
        })
    except Exception as exc:
        result["error"] = str(exc)[:100]
    finally:
        for handle in (ssock, sock):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass
    return result


def apply_throughput(item: dict, throughput: dict) -> dict:
    item["throughput_mbps"] = throughput.get("mbps") if throughput.get("ok") else None
    item["throughput_bytes"] = throughput.get("bytes")
    item["throughput_ttfb_ms"] = throughput.get("ttfb_ms")
    item["throughput_duration_ms"] = throughput.get("duration_ms")
    item["throughput_complete"] = bool(throughput.get("complete"))
    item["throughput_attempted"] = True
    return item


def throughput_pool(valid: list[dict], previous_ip: str | None) -> list[dict]:
    """Top N（按廉价 RTT 排序）+ 当前主 IP，当前主 IP 无论排多少都要测。"""
    pool: list[dict] = []
    seen: set[str] = set()
    for item in valid[:THROUGHPUT_TOP_N]:
        ip = item.get("ip")
        if ip and ip not in seen:
            seen.add(ip)
            pool.append(item)
    if previous_ip and previous_ip not in seen:
        by_ip = {x.get("ip"): x for x in valid}
        extra = by_ip.get(previous_ip)
        if extra is not None:
            seen.add(previous_ip)
            pool.append(extra)
    return pool


def transfer_ms(mbps) -> float:
    """传 THROUGHPUT_BYTES 需要多少毫秒；未测速返回未知占位，保证排在测过速的后面。"""
    if not isinstance(mbps, (int, float)) or mbps <= 0:
        return THROUGHPUT_UNKNOWN_MS
    return THROUGHPUT_BYTES / (float(mbps) * 1_000_000) * 1000


def combine_throughput(ci_mbps, local_mbps, history_mbps):
    """CI 实测、本地实测、历史均值合并；本地权重更高，因为它代表真实使用网络。"""
    parts: list[tuple[float, float]] = []
    if isinstance(ci_mbps, (int, float)) and ci_mbps > 0:
        parts.append((float(ci_mbps), THROUGHPUT_CI_WEIGHT))
    if isinstance(local_mbps, (int, float)) and local_mbps > 0:
        parts.append((float(local_mbps), LOCAL_THROUGHPUT_WEIGHT))
    if parts:
        total = sum(weight for _, weight in parts)
        if total > 0:
            return round(sum(value * weight for value, weight in parts) / total, 3)
    if isinstance(history_mbps, (int, float)) and history_mbps > 0:
        return round(float(history_mbps), 3)
    return None


def exit_info(item: dict) -> dict:
    ex = (((item.get("probe_results") or {}).get("ipv4") or {}).get("exit") or {})
    if ex:
        return ex
    if item.get("method") == "direct_https":
        return {
            "country": item.get("country"),
            "colo": item.get("colo"),
            "botManagement": {
                "score": item.get("cf_bot_score", 95),
                "corporateProxy": False,
                "verifiedBot": False,
            },
        }
    return {}


def default_stability() -> dict:
    """无历史时的保守默认：冷启动统一按 HISTORY_UNKNOWN_RATE 计，不参与质量门槛判负。"""
    return {
        "check_count": 0,
        "success_rate_7d": None,
        "effective_success_rate": HISTORY_UNKNOWN_RATE,
        "avg_latency_ms_recent": None,
        "gate_ok": True,
        "cold_start": True,
    }


def stability_for(history: dict, ip: str) -> dict:
    """把 ip_history 里的滚动窗口换算成排序/门槛可用的稳定性指标。"""
    record = history.get(ip) if isinstance(history, dict) else None
    if not isinstance(record, dict):
        return default_stability()
    recent = str(record.get("recent") or "")
    count = len(recent)
    if count <= 0:
        return default_stability()
    success = recent.count("1")
    raw_rate = success / count
    confidence = min(count / MIN_HISTORY_CHECKS, 1.0)
    effective_rate = raw_rate * confidence + HISTORY_UNKNOWN_RATE * (1.0 - confidence)
    return {
        "check_count": count,
        "success_rate_7d": round(raw_rate, 4),
        "effective_success_rate": round(effective_rate, 4),
        "avg_latency_ms_recent": record.get("avg_latency_ms_recent"),
        "gate_ok": count < MIN_HISTORY_CHECKS or raw_rate >= MIN_SUCCESS_RATE_7D,
        "cold_start": count < MIN_HISTORY_CHECKS,
    }


def enrich(item: dict, source_meta: dict | None = None, stability: dict | None = None) -> dict:
    source_meta = source_meta or {}
    ex = exit_info(item)
    bm = ex.get("botManagement") or {}
    score = bm.get("score")
    corporate = bool(bm.get("corporateProxy"))
    verified = bool(bm.get("verifiedBot"))
    fallback = item.get("method") == "direct_https" or bool(item.get("fallback_unverified"))
    item["sources"] = source_meta.get("sources", [])
    item["source_domains"] = source_meta.get("source_domains", [])
    item["source_type"] = source_meta.get("source_type")
    item["source_count"] = len(item["sources"])
    item["stability"] = stability if isinstance(stability, dict) else default_stability()
    item["risk"] = {
        "cf_bot_score": score,
        "corporate_proxy": corporate,
        "verified_bot": verified,
        "penalty": 0,
        "grade": "fallback_unverified" if fallback else ("low" if score is not None and score >= 90 and not corporate and not verified else "medium"),
        "verification_method": "direct_https" if fallback else "cmliu",
        "asn": ex.get("asn"),
        "as_organization": ex.get("asOrganization") or ex.get("org"),
        "exit_ip": ex.get("ip"),
        "country": ex.get("country") or source_meta.get("country_hint"),
        "city": ex.get("city"),
        "candidate_colo": item.get("colo"),
        "exit_colo": ex.get("colo"),
        "colo": ex.get("colo") or item.get("colo"),
    }
    score_item(item)
    return item



def is_target_region(item: dict) -> bool:
    risk = item.get("risk") or {}
    country = (risk.get("country") or "").upper()
    return not TARGET_COUNTRIES or country in TARGET_COUNTRIES

MAX_PER_ASN = 10  # 同 ASN 最多保留 N 个 IP，防止过于集中
TOP5_MAX_PER_ASN = 1
STANDBY_MAX_PER_ASN = 2
FALLBACK_RANK_PENALTY = 200000

def limit_asn_spread(items: list[dict], max_per_asn: int = MAX_PER_ASN) -> list[dict]:
    """限制同 ASN 最多保留 max_per_n 个 IP，输入已按 rank_key 排序（最优在前）"""
    asn_count: dict[str, int] = {}
    out: list[dict] = []
    for item in items:
        asn = (item.get("risk") or {}).get("asn") or "unknown"
        if asn_count.get(asn, 0) >= max_per_asn:
            continue
        asn_count[asn] = asn_count.get(asn, 0) + 1
        out.append(item)
    return out

def preferred_colo_rank(item: dict) -> int:
    risk = item.get("risk") or {}
    colo = risk.get("exit_colo") or risk.get("colo") or item.get("colo") or ""
    try:
        return PREFERRED_COLOS.index(colo)
    except ValueError:
        return len(PREFERRED_COLOS)

def current_quality_ok(item: dict) -> bool:
    risk = item.get("risk") or {}
    score = risk.get("cf_bot_score")
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    if score is not None and int(score) < CURRENT_MIN_BOT_SCORE:
        return False
    if risk.get("corporate_proxy") or risk.get("verified_bot"):
        return False
    if latency > CURRENT_MAX_LATENCY_MS:
        return False
    mbps = item.get("eff_throughput_mbps")
    if isinstance(mbps, (int, float)) and mbps < THROUGHPUT_MIN_MBPS:
        return False
    stability = item.get("stability")
    if isinstance(stability, dict) and not stability.get("gate_ok", True):
        return False
    return is_target_region(item)


def in_switch_cooldown(previous_state: dict, now_ts: datetime) -> bool:
    ts = previous_state.get("last_switch_at") or previous_state.get("first_selected_at")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now_ts - last).total_seconds() < SWITCH_COOLDOWN_HOURS * 3600


def probe_sample_deficit(item: dict) -> int:
    """已完成探测还差几个样本；未探测返回 0，由延迟门槛兜底。"""
    if not item.get("probe_attempted"):
        return 0
    samples = item.get("rtt_samples_effective")
    if not isinstance(samples, int):
        samples = item.get("rtt_samples") or 0
    return max(0, PROBE_MIN_SAMPLES - samples)


def load_local_probe(now_ts: datetime | None = None) -> dict | None:
    """读取本机 PROXYIP_PROBE_ONLY 产出，超过保质期直接忽略。"""
    if not LOCAL_PROBE_PATH.exists():
        return None
    try:
        data = json.loads(LOCAL_PROBE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    probed_at = parse_timestamp(data.get("probed_at"))
    if probed_at is None:
        return None
    age_hours = ((now_ts or datetime.now(timezone.utc)) - probed_at).total_seconds() / 3600
    if age_hours > LOCAL_PROBE_MAX_AGE_HOURS:
        return None
    data["_age_hours"] = round(age_hours, 2)
    return data


def merge_local_probe(items: list[dict], local: dict | None) -> int:
    """把本机探测按 LOCAL_PROBE_WEIGHT 加权并进 CI 结果，返回命中条数。"""
    if not local:
        return 0
    rtt_by_ip = local.get("rtt") or {}
    throughput_by_ip = local.get("throughput") or {}
    if not isinstance(rtt_by_ip, dict):
        rtt_by_ip = {}
    if not isinstance(throughput_by_ip, dict):
        throughput_by_ip = {}
    weight = LOCAL_PROBE_WEIGHT
    merged = 0
    for item in items:
        ip = item.get("ip")
        if not ip:
            continue
        local_rtt = rtt_by_ip.get(ip)
        local_tp = throughput_by_ip.get(ip)
        hit = False
        if isinstance(local_rtt, dict) and local_rtt.get("rtt_ok"):
            merged += 1
            hit = True
            local_p50 = local_rtt.get("rtt_p50_ms")
            local_samples = int(local_rtt.get("rtt_samples") or 0)
            item["local_rtt_p50_ms"] = local_p50
            item["local_rtt_jitter_ms"] = local_rtt.get("rtt_jitter_ms")
            if isinstance(item.get("rtt_p50_ms"), int) and isinstance(local_p50, int):
                item["rtt_p50_ms"] = int(round(weight * local_p50 + (1 - weight) * item["rtt_p50_ms"]))
            elif isinstance(local_p50, int):
                item["rtt_p50_ms"] = local_p50
                item["rtt_jitter_ms"] = local_rtt.get("rtt_jitter_ms")
                item["rtt_samples"] = local_samples
                item["rtt_ok"] = True
                item["probe_attempted"] = True
            if item.get("rtt_ok"):
                item["latency_ms"] = item.get("rtt_p50_ms")
            ci_samples = int(item.get("rtt_samples") or 0)
            item["rtt_samples_effective"] = max(ci_samples, local_samples)
            local_jitter = local_rtt.get("rtt_jitter_ms")
            if isinstance(local_jitter, int) and isinstance(item.get("rtt_jitter_ms"), int):
                item["rtt_jitter_ms"] = max(item["rtt_jitter_ms"], local_jitter)
        if isinstance(local_tp, dict) and local_tp.get("ok") and local_tp.get("mbps"):
            item["local_throughput_mbps"] = local_tp.get("mbps")
            hit = True
        if hit:
            score_item(item)
    return merged


def refresh_throughput(item: dict, history: dict, local: dict | None) -> None:
    """算出排序用的 eff_throughput_mbps：新鲜实测 > 本机实测 > 72h 内历史。"""
    ip = item.get("ip")
    record = (history or {}).get(ip) or {}
    history_mbps = record.get("mbps")
    history_at = parse_timestamp(record.get("mbps_at"))
    if history_at is not None:
        age_hours = (datetime.now(timezone.utc) - history_at).total_seconds() / 3600
        if age_hours > THROUGHPUT_MAX_AGE_HOURS:
            history_mbps = None
    local_mbps = item.get("local_throughput_mbps")
    if local_mbps is None and isinstance(local, dict):
        entry = (local.get("throughput") or {}).get(ip)
        if isinstance(entry, dict) and entry.get("ok"):
            local_mbps = entry.get("mbps")
    eff = combine_throughput(item.get("throughput_mbps"), local_mbps, history_mbps)
    item["eff_throughput_mbps"] = eff
    if eff is None:
        item["throughput_source"] = None
    elif item.get("throughput_mbps") is not None or local_mbps is not None:
        item["throughput_source"] = "measured"
    else:
        item["throughput_source"] = "history"


def effective_latency_ms(item: dict) -> float:
    """速度 + 抖动 + 采样不足 + 历史失败率折算成毫秒，越小越好。"""
    stability = item.get("stability") if isinstance(item.get("stability"), dict) else default_stability()
    rtt_p50 = item.get("rtt_p50_ms") if isinstance(item.get("rtt_p50_ms"), int) else None
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    base = rtt_p50 if rtt_p50 is not None else latency
    hist_lat = stability.get("avg_latency_ms_recent")
    if rtt_p50 is not None and isinstance(hist_lat, (int, float)):
        base = LATENCY_BLEND_WEIGHT * rtt_p50 + (1.0 - LATENCY_BLEND_WEIGHT) * hist_lat
    jitter = item.get("rtt_jitter_ms") if isinstance(item.get("rtt_jitter_ms"), int) else 0
    success_rate = float(stability.get("effective_success_rate", HISTORY_UNKNOWN_RATE))
    return (
        base
        + JITTER_WEIGHT * jitter
        + (1.0 - success_rate) * STABILITY_PENALTY_MS
        + probe_sample_deficit(item) * PROBE_SAMPLE_DEFICIT_PENALTY_MS
        + THROUGHPUT_WEIGHT * transfer_ms(item.get("eff_throughput_mbps"))
    )


def rank_penalty(item: dict) -> float:
    risk = item.get("risk") or {}
    penalty = effective_latency_ms(item)
    if risk.get("grade") == "fallback_unverified":
        penalty += FALLBACK_RANK_PENALTY
    if risk.get("corporate_proxy"):
        penalty += 50000
    if risk.get("verified_bot"):
        penalty += 50000
    if not is_target_region(item):
        penalty += 100000
    return penalty


def score_item(item: dict) -> dict:
    """排序用的两个指标：penalty 越小越好，stable_score 越大越好。"""
    penalty = rank_penalty(item)
    item.setdefault("risk", {})["penalty"] = penalty
    item["stable_score"] = int(round(1_000_000 - penalty))
    return item


def stable_score(item: dict) -> int:
    return int(round(1_000_000 - rank_penalty(item)))


def passes_quality_gate(item: dict) -> bool:
    """硬门槛：不满足的直接排到后面，门槛内 bot score 不再加分。"""
    risk = item.get("risk") or {}
    score = risk.get("cf_bot_score")
    if score is not None and int(score) < CURRENT_MIN_BOT_SCORE:
        return False
    if risk.get("corporate_proxy") or risk.get("verified_bot"):
        return False
    if risk.get("grade") == "fallback_unverified":
        return False
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    if latency > CURRENT_MAX_LATENCY_MS:
        return False
    jitter = item.get("rtt_jitter_ms") if isinstance(item.get("rtt_jitter_ms"), int) else 0
    if jitter > MAX_JITTER_MS:
        return False
    if item.get("probe_attempted") and probe_sample_deficit(item) > 0:
        return False
    stability = item.get("stability")
    if isinstance(stability, dict) and not stability.get("gate_ok", True):
        return False
    return is_target_region(item)


def rank_key(item: dict) -> tuple:
    risk = item.get("risk") or {}
    return (
        0 if passes_quality_gate(item) else 1,
        float(risk.get("penalty", 999999999)),
        -(risk.get("cf_bot_score") or 0),
        preferred_colo_rank(item),
        -int(item.get("source_count") or 0),
        item.get("latency_ms", 999999),
        item.get("ip", ""),
    )


def read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def read_current_ip() -> str | None:
    if CURRENT_PATH.exists():
        value = CURRENT_PATH.read_text(encoding="utf-8").strip().splitlines()
        if value:
            return value[0].strip()
    state = read_json(STATE_PATH, {})
    return state.get("current_ip")


def select_current(valid: list[dict], all_results: list[dict]) -> tuple[dict, dict, list[dict]]:
    previous_state = read_json(STATE_PATH, {})
    history = read_json(HISTORY_PATH, [])
    previous_ip = previous_state.get("current_ip") or read_current_ip()
    valid_by_ip = {x["ip"]: x for x in valid}
    checked_by_ip = {x.get("ip"): x for x in all_results}
    best = sorted(valid, key=rank_key)
    best_item = best[0] if best else None
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    if previous_ip and previous_ip in valid_by_ip:
        current_item = valid_by_ip[previous_ip]
        quality_ok = current_quality_ok(current_item)
        cooldown = in_switch_cooldown(previous_state, now_dt)
        if quality_ok or cooldown:
            state = {
                **previous_state,
                "current_ip": previous_ip,
                "status": "healthy" if quality_ok else "degraded_quality_cooldown",
                "failure_count": 0,
                "last_success_at": now,
                "last_checked_at": now,
                "last_error": None if quality_ok else "current ip below quality threshold but inside switch cooldown",
                "failover_threshold": FAILOVER_THRESHOLD,
                "quality_threshold": {"min_bot_score": CURRENT_MIN_BOT_SCORE, "max_latency_ms": CURRENT_MAX_LATENCY_MS},
                "switch_cooldown_hours": SWITCH_COOLDOWN_HOURS,
            }
            current_item["selection_reason"] = "kept_current_ip_still_healthy" if quality_ok else "kept_current_ip_quality_cooldown"
            return current_item, state, history

    failure_count = int(previous_state.get("failure_count") or 0) + (1 if previous_ip else 0)
    if previous_ip and failure_count < FAILOVER_THRESHOLD:
        checked = checked_by_ip.get(previous_ip, {})
        current_item = {
            "ip": previous_ip,
            "latency_ms": checked.get("latency_ms"),
            "portRemote": checked.get("portRemote", 443),
            "sources": ["previous_current"],
            "risk": {"grade": "unknown", "cf_bot_score": None, "corporate_proxy": None, "verified_bot": None, "asn": None, "as_organization": None, "country": None, "city": None, "colo": None},
            "selection_reason": "kept_until_failure_threshold",
        }
        state = {
            **previous_state,
            "current_ip": previous_ip,
            "status": "degraded",
            "failure_count": failure_count,
            "last_checked_at": now,
            "last_error": checked.get("error") or "current ip failed validation",
            "failover_threshold": FAILOVER_THRESHOLD,
        }
        return current_item, state, history

    if not best_item:
        raise RuntimeError("No valid ProxyIP candidate available")

    new_ip = best_item["ip"]
    best_item["selection_reason"] = "failover_to_best_candidate" if previous_ip else "initial_best_candidate"
    state = {
        "current_ip": new_ip,
        "status": "healthy",
        "failure_count": 0,
        "first_selected_at": previous_state.get("first_selected_at") if previous_state.get("current_ip") == new_ip else now,
        "last_switch_at": previous_state.get("last_switch_at") if previous_ip == new_ip else now,
        "last_success_at": now,
        "last_checked_at": now,
        "last_error": None,
        "failover_threshold": FAILOVER_THRESHOLD,
        "previous_ip": previous_ip,
    }
    if previous_ip != new_ip:
        history = ([{
            "switched_at": now,
            "from": previous_ip,
            "to": new_ip,
            "reason": "current_failed_threshold" if previous_ip else "initial_selection",
            "failure_count": failure_count,
            "new_score": best_item.get("stable_score"),
            "new_risk": best_item.get("risk"),
        }] + history)[:100]
    return best_item, state, history


def slim_item(item: dict) -> dict:
    risk = item.get("risk") or {}
    return {
        "ip": item.get("ip"),
        "latency_ms": item.get("latency_ms"),
        "rtt_p50_ms": item.get("rtt_p50_ms"),
        "rtt_jitter_ms": item.get("rtt_jitter_ms"),
        "rtt_samples": item.get("rtt_samples"),
        "rtt_ok": item.get("rtt_ok"),
        "api_latency_ms": item.get("api_latency_ms"),
        "throughput_mbps": item.get("throughput_mbps"),
        "eff_throughput_mbps": item.get("eff_throughput_mbps"),
        "throughput_source": item.get("throughput_source"),
        "local_rtt_p50_ms": item.get("local_rtt_p50_ms"),
        "stability": item.get("stability"),
        "portRemote": item.get("portRemote", 443),
        "colo": risk.get("colo") or item.get("colo"),
        "sources": item.get("sources", []),
        "source_domains": item.get("source_domains", []),
        "source_type": item.get("source_type"),
        "source_count": item.get("source_count", len(item.get("sources", []))),
        "stable_score": item.get("stable_score"),
        "verification_method": risk.get("verification_method"),
        "selection_reason": item.get("selection_reason"),
        "risk": {
            "cf_bot_score": risk.get("cf_bot_score"),
            "grade": risk.get("grade"),
            "corporate_proxy": risk.get("corporate_proxy"),
            "verified_bot": risk.get("verified_bot"),
            "asn": risk.get("asn"),
            "as_organization": risk.get("as_organization"),
            "country": risk.get("country"),
            "city": risk.get("city"),
            "candidate_colo": risk.get("candidate_colo"),
            "exit_colo": risk.get("exit_colo"),
            "penalty": risk.get("penalty"),
        },
    }


def asn_key(item: dict) -> str:
    return str((item.get("risk") or {}).get("asn") or "unknown")


def diverse_candidates(items: list[dict], current: dict, count: int, max_per_asn: int) -> list[dict]:
    selected: list[dict] = []
    asn_count: dict[str, int] = {}
    current_asn = asn_key(current)
    if current_asn != "unknown":
        asn_count[current_asn] = 1
    for item in items:
        if item.get("ip") == current.get("ip"):
            continue
        asn = asn_key(item)
        if asn_count.get(asn, 0) >= max_per_asn:
            continue
        selected.append(item)
        asn_count[asn] = asn_count.get(asn, 0) + 1
        if len(selected) >= count:
            break
    return selected


def parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def decode_latencies(raw) -> list[int]:
    if not isinstance(raw, str) or not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or part == "x":
            continue
        try:
            out.append(int(float(part)))
        except ValueError:
            continue
    return out


def encode_latencies(values: list[int]) -> str:
    return ",".join(str(int(x)) for x in values)


def history_from_legacy(record: dict) -> dict | None:
    """旧格式 {checks: [...]} 转成紧凑滚动窗口，避免历史文件无限膨胀。"""
    checks = [x for x in (record.get("checks") or []) if isinstance(x, dict)]
    if not checks:
        return None
    recent = "".join("1" if x.get("success") else "0" for x in checks)[-HISTORY_WINDOW:]
    lats = [int(x.get("latency_ms")) for x in checks if isinstance(x.get("latency_ms"), (int, float))]
    return {
        "recent": recent,
        "lats": encode_latencies(lats[-HISTORY_LAT_WINDOW:]),
        "last_checked_at": record.get("last_checked_at"),
    }


def normalize_history_record(record) -> dict | None:
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("checks"), list):
        record = history_from_legacy(record)
        if record is None:
            return None
    recent = "".join(ch for ch in str(record.get("recent") or "") if ch in "01")[-HISTORY_WINDOW:]
    if not recent:
        return None
    lats = decode_latencies(record.get("lats"))[-HISTORY_LAT_WINDOW:]
    return {
        "recent": recent,
        "lats": encode_latencies(lats),
        "success_rate_7d": round(recent.count("1") / len(recent), 4),
        "avg_latency_ms_recent": round(sum(lats) / len(lats), 2) if lats else None,
        "last_checked_at": record.get("last_checked_at"),
        "mbps": _clean_mbps(record.get("mbps")),
        "mbps_at": record.get("mbps_at") if isinstance(record.get("mbps_at"), str) else None,
    }


def _clean_mbps(value):
    if isinstance(value, dict):
        if not value.get("ok"):
            return None
        value = value.get("mbps")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(float(value), 3)


def prune_ip_history(history: dict) -> dict:
    """规范化并丢掉超过保留期没被巡检到的 IP，历史文件只增不减会失控。"""
    cutoff = time.time() - HISTORY_RETENTION_DAYS * 24 * 60 * 60
    out: dict[str, dict] = {}
    for ip, record in history.items():
        normalized = normalize_history_record(record)
        if normalized is None:
            continue
        parsed = parse_timestamp(normalized.get("last_checked_at"))
        if parsed is None or parsed.timestamp() < cutoff:
            continue
        out[ip] = normalized
    return out


def load_ip_history() -> dict:
    if not IP_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(IP_HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return prune_ip_history(data)


def ip_history_success(item: dict) -> tuple[bool, int | None]:
    """success = 验证通过且本地握手成功，不受地区过滤和 ASN 截断影响。"""
    verified = item.get("success") is True and item.get("supports_ipv4") is True
    if not verified:
        return False, None
    if not item.get("probe_attempted"):
        return True, None
    ok = item.get("rtt_ok") is True
    latency = item.get("rtt_p50_ms") if ok and isinstance(item.get("rtt_p50_ms"), int) else None
    return ok, latency


def update_ip_history(results: list[dict], checked_at: str, throughput_by_ip: dict | None = None) -> dict:
    history = load_ip_history()
    by_ip = {item.get("ip"): item for item in results if item.get("ip")}
    throughput_by_ip = throughput_by_ip or {}
    for ip in sorted(by_ip):
        ok, latency = ip_history_success(by_ip[ip])
        record = history.get(ip) or {}
        recent = (str(record.get("recent") or "") + ("1" if ok else "0"))[-HISTORY_WINDOW:]
        lats = decode_latencies(record.get("lats"))
        if latency is not None:
            lats.append(int(latency))
        lats = lats[-HISTORY_LAT_WINDOW:]
        mbps, mbps_at = _fresh_throughput(record, checked_at, throughput_by_ip.get(ip))
        history[ip] = {
            "recent": recent,
            "lats": encode_latencies(lats),
            "success_rate_7d": round(recent.count("1") / len(recent), 4),
            "avg_latency_ms_recent": round(sum(lats) / len(lats), 2) if lats else None,
            "last_checked_at": checked_at,
            "mbps": mbps,
            "mbps_at": mbps_at,
        }
    return history


def _fresh_throughput(record: dict, checked_at: str, measured):
    """本次测到就刷新，否则沿用未过期的历史；过期的吞吐没有参考价值。"""
    fresh = _clean_mbps(measured)
    if fresh is not None:
        return fresh, checked_at
    stale = _clean_mbps(record.get("mbps"))
    at = record.get("mbps_at") if isinstance(record.get("mbps_at"), str) else None
    parsed = parse_timestamp(at)
    if stale is None or parsed is None:
        return None, None
    if (datetime.now(timezone.utc) - parsed).total_seconds() > THROUGHPUT_MAX_AGE_HOURS * 3600:
        return None, None
    return stale, at


def write_outputs(out: dict, current: dict, state: dict, history: list[dict], throughput_by_ip: dict | None = None) -> None:
    DOCS.mkdir(exist_ok=True)
    valid = out["valid_ips"]
    ips = [x["ip"] for x in valid]
    standby = diverse_candidates(valid, current, STANDBY_COUNT, STANDBY_MAX_PER_ASN)
    recommended_top5 = [current] + diverse_candidates(valid, current, 4, TOP5_MAX_PER_ASN)
    top5 = [x["ip"] for x in recommended_top5 if x.get("ip")]

    (DOCS / "all.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (DOCS / "us.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (DOCS / "best.txt").write_text("\n".join(ips[:BEST_COUNT]) + ("\n" if ips else ""), encoding="utf-8")
    (DOCS / "standby.txt").write_text("\n".join(x["ip"] for x in standby) + ("\n" if standby else ""), encoding="utf-8")
    (DOCS / "top5.txt").write_text("\n".join(top5) + ("\n" if top5 else ""), encoding="utf-8")
    (DOCS / "current.txt").write_text(current["ip"] + "\n", encoding="utf-8")
    base64_body = base64.b64encode("\n".join(ips).encode()).decode()
    (DOCS / "base64.txt").write_text(base64_body, encoding="utf-8")
    (DOCS / "v2ray.txt").write_text(base64_body, encoding="utf-8")
    (DOCS / "current.json").write_text(json.dumps({"current": slim_item(current), "state": state}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    ip_history = update_ip_history(
        out.get("all_results", []),
        out.get("summary", {}).get("checked_at") or now_iso(),
        throughput_by_ip,
    )
    (DOCS / "ip_history.json").write_text(json.dumps(ip_history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    public_out = {k: v for k, v in out.items() if k not in {"all_results", "throughput_by_ip"}}
    (DOCS / "full.json").write_text(json.dumps({**public_out, "current": current, "standby": standby, "state": state, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "dns-records.json").write_text(json.dumps([{
        "type": "A",
        "name": "proxyip",
        "content": current["ip"],
        "proxied": False,
        "ttl": 300,
        "risk": current.get("risk"),
        "latency_ms": current.get("latency_ms"),
        "port": current.get("portRemote", 443),
        "selection_reason": current.get("selection_reason"),
    }], ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "kv-manifest.json").write_text(json.dumps({
        "result_json": "docs/full.json",
        "current_json": "docs/current.json",
        "current_txt": "docs/current.txt",
        "standby_txt": "docs/standby.txt",
        "top5_txt": "docs/top5.txt",
        "all_txt": "docs/all.txt",
        "us_txt": "docs/us.txt",
        "best_txt": "docs/best.txt",
        "base64_txt": "docs/base64.txt",
        "v2ray_txt": "docs/v2ray.txt",
        "history_json": "docs/history.json",
        "ip_history_json": "docs/ip_history.json",
        "state_json": "docs/state.json",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def run_throughput(items: list[dict]) -> dict:
    """对筛选后的池子逐个真实下载测速，返回 ip -> 原始结果。"""
    outcomes: dict[str, dict] = {}
    total = len(items)

    def finish(index: int, item: dict, throughput: dict) -> None:
        apply_throughput(item, throughput)
        outcomes[item["ip"]] = throughput
        detail = f"{throughput.get('mbps')} MB/s" if throughput.get("ok") else f"failed: {throughput.get('error')}"
        print(f"throughput {index}/{total} {item['ip']} {detail}", flush=True)

    if total > 1 and THROUGHPUT_WORKERS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=THROUGHPUT_WORKERS) as pool:
            futures = {pool.submit(probe_throughput, item["ip"]): (i, item) for i, item in enumerate(items, 1)}
            for fut in concurrent.futures.as_completed(futures):
                index, item = futures[fut]
                finish(index, item, fut.result())
    else:
        for index, item in enumerate(items, 1):
            finish(index, item, probe_throughput(item["ip"]))
    return outcomes


def probe_only_main() -> None:
    """PROXYIP_PROBE_ONLY=1：只在本机跑探测，产出 docs/probe_local.json，不动任何 CI 状态。"""
    ips = [x.strip() for x in (DOCS / "all.txt").read_text(encoding="utf-8").splitlines() if x.strip()] if (DOCS / "all.txt").exists() else []
    if not ips:
        full = read_json(DOCS / "full.json", {})
        ips = [x.get("ip") for x in full.get("valid_ips", []) if x.get("ip")]
    if not ips:
        raise SystemExit("PROBE_ONLY needs docs/all.txt or docs/full.json from a previous CI run")

    print(f"probe-only: {len(ips)} ips from docs/all.txt", flush=True)
    rtt: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(probe_rtt, ip): ip for ip in ips}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            ip = futures[fut]
            rtt[ip] = fut.result()
            done += 1
            if done % 25 == 0 or done == len(ips):
                print(f"rtt {done}/{len(ips)}", flush=True)

    ordered = sorted(
        ips,
        key=lambda ip: (0 if rtt[ip].get("rtt_ok") else 1, rtt[ip].get("rtt_p50_ms") or 999999, ip),
    )
    current = read_current_ip()
    pool_ips = ordered[:THROUGHPUT_TOP_N]
    if current and current in ips and current not in pool_ips:
        pool_ips.append(current)

    throughput: dict[str, dict] = {}
    if pool_ips:
        print(f"throughput probing {len(pool_ips)} ips ({THROUGHPUT_BYTES} bytes each)", flush=True)
        for index, ip in enumerate(pool_ips, 1):
            outcome = probe_throughput(ip)
            throughput[ip] = outcome
            detail = f"{outcome.get('mbps')} MB/s" if outcome.get("ok") else f"failed: {outcome.get('error')}"
            print(f"throughput {index}/{len(pool_ips)} {ip} {detail}", flush=True)

    payload = {
        "probed_at": now_iso(),
        "site": "local",
        "rtt": rtt,
        "throughput": {ip: out for ip, out in throughput.items() if out.get("ok")},
        "count": len(rtt),
        "throughput_count": len([x for x in throughput.values() if x.get("ok")]),
        "settings": {
            "rtt_samples": PROBE_SAMPLES,
            "throughput_bytes": THROUGHPUT_BYTES,
            "throughput_top_n": THROUGHPUT_TOP_N,
            "rtt_weight": LOCAL_PROBE_WEIGHT,
            "throughput_weight": LOCAL_THROUGHPUT_WEIGHT,
            "max_age_hours": LOCAL_PROBE_MAX_AGE_HOURS,
        },
    }
    DOCS.mkdir(exist_ok=True)
    LOCAL_PROBE_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {LOCAL_PROBE_PATH}", flush=True)
    print("commit docs/probe_local.json so CI can merge it as weighted data", flush=True)


def main() -> None:
    if PROBE_ONLY:
        probe_only_main()
        return

    candidates, source_stats = collect_candidates()
    print(f"ProxyIP candidates: {len(candidates)}")
    by_ip = {x["ip"]: x for x in candidates}
    ip_history = load_ip_history()
    print(f"stability history loaded: {len(ip_history)} ips")
    results = []
    verified = []
    success_not_ipv4 = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_with_fallback, row["ip"]): row["ip"] for row in candidates}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            results.append(item)
            if item.get("success") is True and item.get("supports_ipv4") is True:
                verified.append(enrich(item, by_ip.get(item["ip"]), stability_for(ip_history, item["ip"])))
            elif item.get("success") is True:
                success_not_ipv4 += 1
            done += 1
            if done % 25 == 0 or done == len(candidates):
                print(f"checked {done}/{len(candidates)} ipv4_valid={len(verified)}")

    pre_region_valid_count = len(verified)
    if verified:
        print(f"probing {len(verified)} verified candidates with {PROBE_SAMPLES} local TCP+TLS samples")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(probe_rtt, item["ip"]): item for item in verified}
        probed = 0
        for fut in concurrent.futures.as_completed(futures):
            item = futures[fut]
            apply_probe(item, fut.result())
            score_item(item)
            probed += 1
            if probed % 50 == 0 or probed == len(verified):
                print(f"probed {probed}/{len(verified)}")

    previous_ip = read_json(STATE_PATH, {}).get("current_ip") or read_current_ip()
    local_probe = load_local_probe()
    merged_local = merge_local_probe(verified, local_probe) if local_probe else 0
    if local_probe:
        print(
            f"local probe overlay: rtt={merged_local} ips, age={local_probe.get('_age_hours')}h, "
            f"weight={LOCAL_PROBE_WEIGHT} (never trusted beyond {LOCAL_PROBE_MAX_AGE_HOURS}h)",
            flush=True,
        )
        for item in verified:
            score_item(item)

    valid = [x for x in verified if is_target_region(x)]
    valid.sort(key=rank_key)

    # Stage 3: 廉价 RTT 先筛出 Top N，再对这个小池子做真实下载测速。
    throughput_pool_items = throughput_pool(valid, previous_ip)
    throughput_by_ip: dict[str, dict] = {}
    if throughput_pool_items:
        print(
            f"throughput screening {len(throughput_pool_items)}/{len(valid)} candidates "
            f"({THROUGHPUT_BYTES} bytes each, workers={THROUGHPUT_WORKERS})",
            flush=True,
        )
        throughput_by_ip = run_throughput(throughput_pool_items)

    for item in verified:
        refresh_throughput(item, ip_history, local_probe)
        score_item(item)

    valid = [x for x in verified if is_target_region(x)]
    valid.sort(key=rank_key)
    valid = limit_asn_spread(valid)
    current, state, switch_history = select_current(valid, results)
    out = {
        "summary": {
            "source_count": len(source_stats),
            "sources": source_stats,
            "candidate_filter": "third-party ProxyIP only; Cloudflare official IP ranges excluded; IPv4 only; text/domain sources, manual allowlist and denylist supported; target exit region enforced",
            "target_countries": sorted(TARGET_COUNTRIES),
            "preferred_colos": PREFERRED_COLOS,
            "selection_policy": "single stable current IP; keep while healthy and still in target region; fail over only after consecutive validation failures",
            "ranking": "three stages: quality gate first (bot score >= min, target region, no corporateProxy/verifiedBot, third-party verification, local handshake latency <= max, jitter <= max, enough probe samples, 7d success rate), then effective latency = blended local P50 + jitter weight + stability penalty + transfer time of a fixed payload; bot score only breaks ties inside the gate",
            "latency_metric": "local TCP+TLS handshake P50 in ms; third-party checker round trip kept as api_latency_ms; measured download throughput folded in as transfer time",
            "probe": {
                "samples": PROBE_SAMPLES,
                "timeout_s": PROBE_TIMEOUT,
                "min_samples": PROBE_MIN_SAMPLES,
                "max_jitter_ms": MAX_JITTER_MS,
                "jitter_weight": JITTER_WEIGHT,
                "sample_deficit_penalty_ms": PROBE_SAMPLE_DEFICIT_PENALTY_MS,
            },
            "throughput": {
                "top_n": THROUGHPUT_TOP_N,
                "bytes": THROUGHPUT_BYTES,
                "timeout_s": THROUGHPUT_TIMEOUT,
                "weight": THROUGHPUT_WEIGHT,
                "unknown_ms": THROUGHPUT_UNKNOWN_MS,
                "min_mbps_current": THROUGHPUT_MIN_MBPS,
                "max_age_hours": THROUGHPUT_MAX_AGE_HOURS,
                "ci_weight": THROUGHPUT_CI_WEIGHT,
                "pool": len(throughput_pool_items),
                "measured": len([x for x in throughput_by_ip.values() if x.get("ok")]),
                "policy": "cheap RTT screens candidates down to top N, then a real download through the ProxyIP supplies the transfer-time term; a fast handshake with a slow pipe loses",
            },
            "local_probe": {
                "present": bool(local_probe),
                "age_hours": local_probe.get("_age_hours") if local_probe else None,
                "merged_rtt": merged_local,
                "weight": LOCAL_PROBE_WEIGHT,
                "throughput_weight": LOCAL_THROUGHPUT_WEIGHT,
                "max_age_hours": LOCAL_PROBE_MAX_AGE_HOURS,
            },
            "stability_history": {
                "window_checks": HISTORY_WINDOW,
                "latency_window": HISTORY_LAT_WINDOW,
                "retention_days": HISTORY_RETENTION_DAYS,
                "min_checks": MIN_HISTORY_CHECKS,
                "min_success_rate_7d": MIN_SUCCESS_RATE_7D,
                "cold_start_rate": HISTORY_UNKNOWN_RATE,
                "penalty_ms": STABILITY_PENALTY_MS,
                "ips_loaded": len(ip_history),
            },
            "total_candidates": len(candidates),
            "cmliu_ipv4_valid_before_region_filter": pre_region_valid_count,
            "cmliu_ipv4_valid": len(valid),
            "cmliu_success_not_ipv4": success_not_ipv4,
            "current_ip": current["ip"],
            "checked_at": now_iso(),
            "checker": CHECK_API,
        },
        "recommended_top5": [current] + diverse_candidates(valid, current, 4, TOP5_MAX_PER_ASN),
        "valid_ips": valid,
        "all_results": results,
        "throughput_by_ip": throughput_by_ip,
    }
    public_for_result = {k: v for k, v in out.items() if k != "throughput_by_ip"}
    Path("result.json").write_text(json.dumps({**public_for_result, "current": current, "state": state, "history": switch_history}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_outputs(out, current, state, switch_history, throughput_by_ip)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print("Current ProxyIP:", current["ip"], current.get("selection_reason"))
    print("Standby:", [x["ip"] for x in valid if x["ip"] != current["ip"]][:5])


if __name__ == "__main__":
    main()
