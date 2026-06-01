#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import ipaddress
import json
import os
import re
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = [
    {"name": "zip.cm.edu.kg/all.txt", "url": "https://zip.cm.edu.kg/all.txt", "countries": {"US"}, "ports": {443}},
    {"name": "addressesapi.090227", "url": "https://addressesapi.090227.xyz/ip.txt", "countries": None, "ports": {443}},
    {"name": "090227-CloudFlareYes", "url": "https://addressesapi.090227.xyz/CloudFlareYes", "countries": None, "ports": {443}},
    {"name": "xiaoji-cf_cdn_ip", "url": "https://raw.githubusercontent.com/xiaoji235/airport-free/main/airport/cf_cdn_ip.txt", "countries": None, "ports": {443}},
    {"name": "Alvin9999-cloudflare", "url": "https://raw.githubusercontent.com/Alvin9999/new-pac/master/cloudflare-ip.txt", "countries": None, "ports": {443}},
]
CHECK_API = "https://api.090227.xyz/check"
USER_AGENT = "cf-proxyip-stable-builder/2.0"
MAX_WORKERS = int(os.environ.get("PROXYIP_MAX_WORKERS", "24"))
TIMEOUT = int(os.environ.get("PROXYIP_CHECK_TIMEOUT", "35"))
MAX_CANDIDATES = int(os.environ.get("PROXYIP_MAX_CANDIDATES", "1400"))
FAILOVER_THRESHOLD = int(os.environ.get("PROXYIP_FAILOVER_THRESHOLD", "2"))
FALLBACK_SOURCES: list[dict] = []
TARGET_COUNTRIES = {x.strip().upper() for x in os.environ.get("PROXYIP_TARGET_COUNTRIES", "US").split(",") if x.strip()}
PREFERRED_COLOS = [x.strip().upper() for x in os.environ.get("PROXYIP_PREFERRED_COLOS", "IAD").split(",") if x.strip()]
BEST_COUNT = 20
STANDBY_COUNT = 10

IP_RE = re.compile(r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?::(?P<port>\d{1,5}))?(?:#(?P<country>[A-Z]{2}))?")
DOCS = Path("docs")
STATE_PATH = DOCS / "state.json"
CURRENT_PATH = DOCS / "current.txt"
HISTORY_PATH = DOCS / "history.json"
MANUAL_ALLOWLIST = Path("allowlist.txt")
MANUAL_DENYLIST = Path("denylist.txt")


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
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False


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


def collect_candidates() -> tuple[list[dict], list[dict]]:
    by_ip: dict[str, dict] = {}
    source_stats: list[dict] = []
    for src in SOURCES:
        try:
            text = fetch_text(src["url"])
            rows = parse_candidates(text, src["name"], src.get("countries"), src.get("ports"))
            source_stats.append({"name": src["name"], "url": src["url"], "count": len(rows), "error": None})
        except Exception as exc:
            rows = []
            source_stats.append({"name": src["name"], "url": src["url"], "count": 0, "error": str(exc)})
        for row in rows:
            item = by_ip.setdefault(row["ip"], {"ip": row["ip"], "port": row.get("port", 443), "country_hint": row.get("country_hint"), "sources": []})
            item["sources"] = sorted(set(item["sources"]) | set(row.get("sources") or []))

    for row in read_ip_file(MANUAL_ALLOWLIST, "manual_allowlist"):
        item = by_ip.setdefault(row["ip"], {"ip": row["ip"], "port": row.get("port", 443), "country_hint": row.get("country_hint"), "sources": []})
        item["sources"] = sorted(set(item["sources"]) | {"manual_allowlist"})

    current = read_current_ip()
    if current and valid_ipv4(current):
        item = by_ip.setdefault(current, {"ip": current, "port": 443, "country_hint": None, "sources": []})
        item["sources"] = sorted(set(item["sources"]) | {"current_dns"})

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
    import ssl
    import socket
    
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
        
        is_cf = "cf-ray" in resp_text.lower() or "cloudflare" in resp_text.lower()
        is_200 = "HTTP/1.1 200" in resp_text or "HTTP/2 200" in resp_text
        
        country = None
        for line in resp_text.split("\n"):
            if line.startswith("loc="):
                country = line.split("=")[1].strip()
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
            }
        else:
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


def exit_info(item: dict) -> dict:
    return (((item.get("probe_results") or {}).get("ipv4") or {}).get("exit") or {})


def enrich(item: dict, source_meta: dict | None = None) -> dict:
    source_meta = source_meta or {}
    ex = exit_info(item)
    bm = ex.get("botManagement") or {}
    score = bm.get("score")
    corporate = bool(bm.get("corporateProxy"))
    verified = bool(bm.get("verifiedBot"))
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    penalty = (100 - int(score or 0)) * 1000 + (50000 if corporate else 0) + (50000 if verified else 0) + latency
    item["sources"] = source_meta.get("sources", [])
    item["source_count"] = len(item["sources"])
    item["risk"] = {
        "cf_bot_score": score,
        "corporate_proxy": corporate,
        "verified_bot": verified,
        "penalty": penalty,
        "grade": "low" if score is not None and score >= 90 and not corporate and not verified else "medium",
        "asn": ex.get("asn"),
        "as_organization": ex.get("asOrganization") or ex.get("org"),
        "exit_ip": ex.get("ip"),
        "country": ex.get("country") or source_meta.get("country_hint"),
        "city": ex.get("city"),
        "colo": item.get("colo") or ex.get("colo"),
    }
    item["stable_score"] = stable_score(item)
    return item



def is_target_region(item: dict) -> bool:
    risk = item.get("risk") or {}
    country = (risk.get("country") or "").upper()
    return not TARGET_COUNTRIES or country in TARGET_COUNTRIES

MAX_PER_ASN = 10  # 同 ASN 最多保留 N 个 IP，防止过于集中

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
    colo = risk.get("colo") or item.get("colo") or ""
    try:
        return PREFERRED_COLOS.index(colo)
    except ValueError:
        return len(PREFERRED_COLOS)

def stable_score(item: dict) -> int:
    risk = item.get("risk") or {}
    score = risk.get("cf_bot_score") or 0
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    source_bonus = min(item.get("source_count") or 0, 4) * 50
    return int(score) * 1000 - latency - (50000 if risk.get("corporate_proxy") else 0) - (50000 if risk.get("verified_bot") else 0) + source_bonus


def rank_key(item: dict) -> tuple:
    risk = item.get("risk") or {}
    return (
        risk.get("penalty", 999999999),
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
    now = now_iso()

    if previous_ip and previous_ip in valid_by_ip:
        current_item = valid_by_ip[previous_ip]
        state = {
            **previous_state,
            "current_ip": previous_ip,
            "status": "healthy",
            "failure_count": 0,
            "last_success_at": now,
            "last_checked_at": now,
            "last_error": None,
            "failover_threshold": FAILOVER_THRESHOLD,
        }
        current_item["selection_reason"] = "kept_current_ip_still_healthy"
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
        "portRemote": item.get("portRemote", 443),
        "colo": risk.get("colo") or item.get("colo"),
        "sources": item.get("sources", []),
        "source_count": item.get("source_count", len(item.get("sources", []))),
        "stable_score": item.get("stable_score"),
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
        },
    }


def write_outputs(out: dict, current: dict, state: dict, history: list[dict]) -> None:
    DOCS.mkdir(exist_ok=True)
    valid = out["valid_ips"]
    ips = [x["ip"] for x in valid]
    standby = [x for x in valid if x["ip"] != current["ip"]][:STANDBY_COUNT]
    top5 = [x["ip"] for x in ([current] + standby)[:5] if x.get("ip")]

    (DOCS / "all.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    shutil.copy(DOCS / "all.txt", DOCS / "us.txt")
    (DOCS / "best.txt").write_text("\n".join(ips[:BEST_COUNT]) + ("\n" if ips else ""), encoding="utf-8")
    (DOCS / "standby.txt").write_text("\n".join(x["ip"] for x in standby) + ("\n" if standby else ""), encoding="utf-8")
    (DOCS / "top5.txt").write_text("\n".join(top5) + ("\n" if top5 else ""), encoding="utf-8")
    (DOCS / "current.txt").write_text(current["ip"] + "\n", encoding="utf-8")
    (DOCS / "v2ray.txt").write_text(base64.b64encode("\n".join(ips).encode()).decode(), encoding="utf-8")
    (DOCS / "current.json").write_text(json.dumps({"current": slim_item(current), "state": state}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "full.json").write_text(json.dumps({**out, "current": current, "standby": standby, "state": state, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
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


def main() -> None:
    candidates, source_stats = collect_candidates()
    print(f"ProxyIP candidates: {len(candidates)}")
    by_ip = {x["ip"]: x for x in candidates}
    results = []
    valid = []
    success_not_ipv4 = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_with_fallback, row["ip"]): row["ip"] for row in candidates}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            results.append(item)
            if item.get("success") is True and item.get("supports_ipv4") is True:
                valid.append(enrich(item, by_ip.get(item["ip"])))
            elif item.get("success") is True:
                success_not_ipv4 += 1
            done += 1
            if done % 25 == 0 or done == len(candidates):
                print(f"checked {done}/{len(candidates)} ipv4_valid={len(valid)}")

    pre_region_valid_count = len(valid)
    valid = [x for x in valid if is_target_region(x)]
    valid.sort(key=rank_key)
    valid = limit_asn_spread(valid)
    current, state, history = select_current(valid, results)
    out = {
        "summary": {
            "source_count": len(SOURCES),
            "sources": source_stats,
            "candidate_filter": "IPv4 only, US/443 for default source, manual allowlist supported, denylist supported, target exit region enforced",
            "target_countries": sorted(TARGET_COUNTRIES),
            "preferred_colos": PREFERRED_COLOS,
            "selection_policy": "single stable current IP; keep while healthy and still in target region; fail over only after consecutive validation failures",
            "ranking": "lowest risk first: Cloudflare bot score, preferred colo, no corporateProxy, no verifiedBot, source count, lower latency",
            "total_candidates": len(candidates),
            "cmliu_ipv4_valid_before_region_filter": pre_region_valid_count,
            "cmliu_ipv4_valid": len(valid),
            "cmliu_success_not_ipv4": success_not_ipv4,
            "current_ip": current["ip"],
            "checked_at": now_iso(),
            "checker": CHECK_API,
        },
        "recommended_top5": [current] + [x for x in valid if x["ip"] != current["ip"]][:4],
        "valid_ips": valid,
        "all_results": results,
    }
    Path("result.json").write_text(json.dumps({**out, "current": current, "state": state, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_outputs(out, current, state, history)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print("Current ProxyIP:", current["ip"], current.get("selection_reason"))
    print("Standby:", [x["ip"] for x in valid if x["ip"] != current["ip"]][:5])


if __name__ == "__main__":
    main()
