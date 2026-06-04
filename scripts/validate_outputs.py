#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TARGET_COUNTRIES = {x.strip().upper() for x in os.environ.get("PROXYIP_TARGET_COUNTRIES", "US").split(",") if x.strip()}
PROXYIP_RECORD_NAME = os.environ.get("PROXYIP_RECORD_NAME", "proxyip.leilaomi.cc.cd")


def read_json(name: str):
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def read_lines(name: str) -> list[str]:
    return [x.strip() for x in (DOCS / name).read_text(encoding="utf-8").splitlines() if x.strip()]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_ipv4(value: str, label: str) -> None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} is not an IP address: {value}") from exc
    require(ip.version == 4, f"{label} is not IPv4: {value}")


def main() -> None:
    current_lines = read_lines("current.txt")
    all_ips = read_lines("all.txt")
    top5 = read_lines("top5.txt")
    standby_lines = read_lines("standby.txt")
    full = read_json("full.json")
    current_json = read_json("current.json")
    state = read_json("state.json")
    dns_records = read_json("dns-records.json")

    require(len(current_lines) == 1, "docs/current.txt must contain exactly one IP")
    current = current_lines[0]
    require_ipv4(current, "current.txt")

    require(len(all_ips) >= 5, "docs/all.txt has too few valid IPs")
    require(len(all_ips) == len(set(all_ips)), "docs/all.txt contains duplicate IPs")
    for index, ip in enumerate(all_ips, 1):
        require_ipv4(ip, f"all.txt line {index}")

    require(top5, "docs/top5.txt is empty")
    require(top5[0] == current, "docs/top5.txt first row must match current.txt")
    for index, ip in enumerate(top5, 1):
        require_ipv4(ip, f"top5.txt line {index}")
    require(current not in standby_lines, "docs/standby.txt must not contain current IP")

    summary = full.get("summary", {})
    valid_ips = full.get("valid_ips", [])
    require(summary.get("current_ip") == current, "full.json summary current_ip does not match current.txt")
    require(summary.get("cmliu_ipv4_valid") == len(all_ips), "full.json valid count does not match all.txt")
    require(len(valid_ips) == len(all_ips), "full.json valid_ips length does not match all.txt")
    require([item.get("ip") for item in valid_ips] == all_ips, "full.json valid_ips order does not match all.txt")

    require(state.get("current_ip") == current, "state.json current_ip does not match current.txt")
    require(current_json.get("current", {}).get("ip") == current, "current.json current IP does not match current.txt")

    bad_countries = []
    for item in valid_ips:
        country = (item.get("risk", {}).get("country") or "").upper()
        if TARGET_COUNTRIES and country not in TARGET_COUNTRIES:
            bad_countries.append((item.get("ip"), country))
    require(not bad_countries, f"valid_ips contains non-target countries: {bad_countries[:5]}")

    require(len(dns_records) == 1, "dns-records.json must contain exactly one DNS record")
    record = dns_records[0]
    require(record.get("type") == "A", "dns-records.json record must be A")
    require(record.get("content") == current, "dns-records.json content must match current.txt")
    require(record.get("proxied") is False, "dns-records.json record must be DNS-only/proxied=false")
    require(record.get("ttl") == 300, "dns-records.json ttl must be 300")
    require(PROXYIP_RECORD_NAME.split(".")[0] in {record.get("name"), PROXYIP_RECORD_NAME}, "dns-records.json name does not align with PROXYIP_RECORD_NAME")

    print(json.dumps({"ok": True, "current": current, "valid_count": len(all_ips), "target_countries": sorted(TARGET_COUNTRIES)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
