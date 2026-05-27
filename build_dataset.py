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
USER_AGENT = "cf-proxyip-us-builder/1.0"
MAX_WORKERS = 24
TIMEOUT = 35
MAX_CANDIDATES = 1305

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
        latency = int((time.monotonic() - start) * 1000)
        data["latency_ms"] = latency
        data["ip"] = ip
        return data
    except Exception as exc:
        return {"ip": ip, "success": False, "error": str(exc), "latency_ms": int((time.monotonic() - start) * 1000)}


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
                valid.append(item)
            elif item.get("success") is True:
                success_not_ipv4 += 1
            done += 1
            if done % 25 == 0 or done == len(candidates):
                print(f"checked {done}/{len(candidates)} ipv4_valid={len(valid)}")

    valid.sort(key=lambda x: x.get("latency_ms", 999999))
    ips = [x["ip"] for x in valid]
    out = {
        "summary": {
            "source": SOURCE,
            "candidate_filter": "IPv4 only, #US only, port 443 only, cmliu success=true, supports_ipv4=true",
            "total_candidates": len(candidates),
            "cmliu_ipv4_valid": len(valid),
            "cmliu_success_not_ipv4": success_not_ipv4,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "checker": CHECK_API,
        },
        "valid_ips": valid,
        "all_results": results,
    }
    Path("result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "all.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "us.txt").write_text("\n".join(ips) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "best.txt").write_text("\n".join(ips[:20]) + ("\n" if ips else ""), encoding="utf-8")
    (docs / "v2ray.txt").write_text(base64.b64encode("\n".join(ips).encode()).decode(), encoding="utf-8")
    (docs / "full.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    if ips:
        print("Top valid:", ips[:20])


if __name__ == "__main__":
    main()
