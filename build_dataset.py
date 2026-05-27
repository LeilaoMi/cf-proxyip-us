#!/usr/bin/env python3
from __future__ import annotations
import base64
import concurrent.futures
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = "https://zip.cm.edu.kg/all.txt"
CHECK_API = "https://api.090227.xyz/check"
USER_AGENT = "cf-proxyip-us-builder/1.1"
MAX_WORKERS = 24
TIMEOUT = 35
MAX_CANDIDATES = 1305
TOP5_COUNT = 5
BEST_COUNT = 20

IP_RE = re.compile(r"^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?::(?P<port>\d{1,5}))?#(?P<country>[A-Z]{2})$")


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=45) as res:
        return res.read().decode("utf-8", "ignore")


def us_ipv4_candidates(text: str) -> list[str]:
    seen = set()
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = IP_RE.match(line)
        if not m or m.group("country") != "US":
            continue
        if m.group("port") != "443":
            continue
        ip = m.group("ip")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if ip not in seen:
            seen.add(ip)
            items.append(ip)
    return items


def check_cmliu(ip: str) -> dict:
    url = f"{CHECK_API}?proxyip={ip}"
    start = time.monotonic()
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urlopen(req, timeout=TIMEOUT) as res:
            data = json.loads(res.read().decode("utf-8", "ignore"))
        data["latency_ms"] = int((time.monotonic() - start) * 1000)
        data["ip"] = ip
        return data
    except Exception as exc:
        return {"ip": ip, "success": False, "error": str(exc), "latency_ms": int((time.monotonic() - start) * 1000)}


def exit_info(item: dict) -> dict:
    return (((item.get("probe_results") or {}).get("ipv4") or {}).get("exit") or {})


def enrich(item: dict) -> dict:
    ex = exit_info(item)
    bm = ex.get("botManagement") or {}
    score = bm.get("score")
    corporate = bool(bm.get("corporateProxy"))
    verified = bool(bm.get("verifiedBot"))
    latency = item.get("latency_ms") if isinstance(item.get("latency_ms"), int) else 999999
    risk_penalty = (100 - int(score or 0)) * 1000 + (50000 if corporate else 0) + (50000 if verified else 0) + latency
    item["risk"] = {
        "cf_bot_score": score,
        "corporate_proxy": corporate,
        "verified_bot": verified,
        "penalty": risk_penalty,
        "grade": "low" if score is not None and score >= 90 and not corporate and not verified else "medium",
        "asn": ex.get("asn"),
        "as_organization": ex.get("asOrganization") or ex.get("org"),
        "exit_ip": ex.get("ip"),
        "country": ex.get("country"),
        "city": ex.get("city"),
        "colo": item.get("colo") or ex.get("colo"),
    }
    return item


def rank_key(item: dict) -> tuple:
    risk = item.get("risk") or {}
    score = risk.get("cf_bot_score") or 0
    return (
        risk.get("penalty", 999999999),
        -score,
        item.get("latency_ms", 999999),
        item.get("ip", ""),
    )


def select_top5(valid: list[dict]) -> list[dict]:
    ranked = sorted(valid, key=rank_key)
    picked = []
    seen_asn = set()
    for item in ranked:
        asn = (item.get("risk") or {}).get("asn")
        if asn in seen_asn:
            continue
        picked.append(item)
        seen_asn.add(asn)
        if len(picked) == TOP5_COUNT:
            return picked
    return ranked[:TOP5_COUNT]


def write_outputs(out: dict) -> None:
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    ips = [x["ip"] for x in out["valid_ips"]]
    top5 = [x["ip"] for x in out["recommended_top5"]]
    (docs / "all.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "us.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "best.txt").write_text("\n".join(ips[:BEST_COUNT]) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "top5.txt").write_text("\n".join(top5) + ("\n" if top5 else ""), encoding="utf-8")
    (docs / "v2ray.txt").write_text(base64.b64encode("\n".join(ips).encode()).decode(), encoding="utf-8")
    (docs / "full.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    dns_records = [
        {
            "type": "A",
            "name": "proxyip",
            "content": item["ip"],
            "proxied": False,
            "ttl": 300,
            "risk": item.get("risk"),
            "latency_ms": item.get("latency_ms"),
            "port": item.get("portRemote", 443),
        }
        for item in out["recommended_top5"]
    ]
    (docs / "dns-records.json").write_text(json.dumps(dns_records, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    source_text = fetch_text(SOURCE)
    candidates = us_ipv4_candidates(source_text)[:MAX_CANDIDATES]
    print(f"US IPv4:443 candidates: {len(candidates)}")

    results = []
    valid = []
    success_not_ipv4 = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_cmliu, ip): ip for ip in candidates}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            results.append(item)
            if item.get("success") is True and item.get("supports_ipv4") is True:
                valid.append(enrich(item))
            elif item.get("success") is True:
                success_not_ipv4 += 1
            done += 1
            if done % 25 == 0 or done == len(candidates):
                print(f"checked {done}/{len(candidates)} ipv4_valid={len(valid)}")

    valid.sort(key=rank_key)
    recommended_top5 = select_top5(valid)
    out = {
        "summary": {
            "source": SOURCE,
            "candidate_filter": "IPv4 only, #US only, port 443 only, cmliu success=true, supports_ipv4=true",
            "ranking": "lowest risk first: Cloudflare bot score, no corporateProxy, no verifiedBot, lower latency; top5 keeps ASN diversity",
            "total_candidates": len(candidates),
            "cmliu_ipv4_valid": len(valid),
            "cmliu_success_not_ipv4": success_not_ipv4,
            "recommended_top5_count": len(recommended_top5),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "checker": CHECK_API,
        },
        "recommended_top5": recommended_top5,
        "valid_ips": valid,
        "all_results": results,
    }
    Path("result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    write_outputs(out)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print("Recommended top5:", [x["ip"] for x in recommended_top5])


if __name__ == "__main__":
    main()
