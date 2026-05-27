#!/usr/bin/env python3
"""Check Cloudflare ProxyIP candidates and publish static result files."""

from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import aiohttp
except ImportError:
    print("Missing dependency: aiohttp. Install with: python3 -m pip install aiohttp", file=sys.stderr)
    raise

CONFIG = {
    "test_host": "speed.cloudflare.com",
    "test_path": "/__down?bytes=100",
    "timeout": 6,
    "concurrency": 50,
    "max_latency": 3000,
    "target_countries": ["US"],
    "remote_sources": [
        "https://zip.cm.edu.kg/all.txt",
        "https://addressesapi.090227.xyz/ip.txt",
        "https://addressesapi.090227.xyz/CloudFlareYes",
        "https://raw.githubusercontent.com/xiaoji235/airport-free/main/airport/cf_cdn_ip.txt",
        "https://raw.githubusercontent.com/Alvin9999/new-pac/master/cloudflare-ip.txt",
    ],
    "builtin_ips": [
        "104.16.0.1", "104.17.0.1", "104.18.0.1", "104.19.0.1",
        "104.20.0.1", "104.21.0.1", "104.22.0.1", "104.23.0.1",
        "104.24.0.1", "104.25.0.1", "104.26.0.1", "104.27.0.1",
        "108.162.192.1", "108.162.193.1", "108.162.194.1",
        "162.158.0.1", "162.158.1.1", "162.158.2.1",
        "172.64.0.1", "172.64.1.1", "172.65.0.1", "172.65.1.1",
        "172.66.0.1", "172.67.0.1", "172.67.1.1",
        "173.245.48.1", "173.245.49.1", "173.245.50.1",
        "188.114.96.1", "188.114.97.1", "188.114.98.1",
        "190.93.240.1", "190.93.241.1", "190.93.242.1",
        "197.234.240.1", "197.234.241.1",
        "198.41.128.1", "198.41.129.1",
    ],
}

DOMAIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+\.?$")


def is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return "." in value
    except ValueError:
        return False


def is_ipv4_target(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def is_valid_domain(value: str) -> bool:
    value = value.rstrip(".")
    return len(value) <= 253 and bool(DOMAIN_RE.fullmatch(value))


def normalize_target(value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    value = value.split("#", 1)[0].strip()
    value = re.split(r"[\s,|]+", value, maxsplit=1)[0].strip()
    if "://" in value:
        value = value.split("://", 1)[1].split("/", 1)[0]
    if value.startswith("[") and "]" in value:
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1 and re.search(r":[0-9]+$", value):
        value = value.rsplit(":", 1)[0]
    value = value.strip("[]")
    if is_valid_ip(value) or is_valid_domain(value):
        return value.rstrip(".")
    return None


def extract_targets(text: str) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for raw in re.split(r"[\r\n]+", text):
        target = normalize_target(raw)
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


async def fetch_remote_source(session: aiohttp.ClientSession, url: str) -> list[str]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"  ✗ {url}: HTTP {resp.status}")
                return []
            return extract_targets(await resp.text())
    except Exception as exc:
        print(f"  ✗ {url}: {exc}")
        return []


def default_fofa_query(country: str) -> str:
    country_code = (country or "US").split(",", 1)[0].strip().upper() or "US"
    return f'server=="cloudflare" && header="Forbidden" && asn!="13335" && asn!="209242" && country="{country_code}" && port="443"'


async def fetch_fofa_candidates(
    session: aiohttp.ClientSession,
    key: str,
    query: str,
    page: int,
    size: int,
    fields: str,
    full: bool,
) -> list[str]:
    qbase64 = base64.b64encode(query.encode("utf-8")).decode("ascii")
    params = {
        "key": key,
        "qbase64": qbase64,
        "fields": fields,
        "page": str(page),
        "size": str(size),
        "r_type": "json",
    }
    if full:
        params["full"] = "true"
    url = "https://fofa.info/api/v1/search/all?" + urlencode(params)

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        print(f"  ✗ FOFA: {exc}")
        return []

    if data.get("error"):
        print(f"  ✗ FOFA: {data.get('errmsg') or data.get('message') or data.get('error')}")
        return []

    field_names = [item.strip() for item in fields.split(",")]
    candidates: list[str] = []
    for row in data.get("results", []):
        if isinstance(row, list):
            record = dict(zip(field_names, row))
            for name in ("ip", "host"):
                target = normalize_target(str(record.get(name, "")))
                if target:
                    candidates.append(target)
        elif isinstance(row, str):
            target = normalize_target(row)
            if target:
                candidates.append(target)

    print(f"  ✓ FOFA: +{len(candidates)} candidates from {data.get('size', len(candidates))} rows")
    return candidates


async def collect_targets(
    files: list[str],
    use_remote: bool,
    use_fofa: bool = False,
    fofa_query: str | None = None,
    fofa_page: int = 1,
    fofa_size: int = 100,
    fofa_fields: str = "ip,host,port",
    fofa_full: bool = False,
    fofa_key: str | None = None,
    country: str = "US",
    ipv4_only: bool = True,
) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []

    def add_many(items: list[str]) -> None:
        for item in items:
            if ipv4_only and not is_ipv4_target(item):
                continue
            if item not in seen:
                seen.add(item)
                targets.append(item)

    add_many(CONFIG["builtin_ips"])

    for filename in files:
        path = Path(filename)
        if not path.exists():
            print(f"  ✗ file not found: {filename}")
            continue
        loaded = extract_targets(path.read_text(encoding="utf-8", errors="ignore"))
        add_many(loaded)
        print(f"📄 {filename}: +{len(loaded)} candidates")

    if use_remote:
        print("📡 Fetching remote sources...")
        async with aiohttp.ClientSession(headers={"User-Agent": "CF-ProxyIP-Checker/2.0"}) as session:
            results = await asyncio.gather(*(fetch_remote_source(session, url) for url in CONFIG["remote_sources"]))
        for url, items in zip(CONFIG["remote_sources"], results):
            before = len(targets)
            add_many(items)
            print(f"  ✓ {url}: +{len(targets) - before}")

    if use_fofa:
        key = fofa_key or os.environ.get("FOFA_KEY")
        if not key:
            print("  ✗ FOFA: missing key. Set FOFA_KEY or pass --fofa-key")
        else:
            query = fofa_query or default_fofa_query(country)
            print(f"🔎 Fetching FOFA candidates: {query}")
            async with aiohttp.ClientSession(headers={"User-Agent": "CF-ProxyIP-Checker/2.0"}) as session:
                items = await fetch_fofa_candidates(session, key, query, fofa_page, fofa_size, fofa_fields, fofa_full)
            add_many(items)

    return targets


async def check_tcp(host: str, timeout: int) -> tuple[bool, int]:
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout=timeout)
        latency = int((time.monotonic() - start) * 1000)
        writer.close()
        await writer.wait_closed()
        reader.feed_eof()
        return True, latency
    except Exception:
        return False, -1


async def check_https(session: aiohttp.ClientSession, target: str, timeout: int) -> dict[str, Any]:
    url = f"https://{target}{CONFIG['test_path']}"
    headers = {
        "Host": CONFIG["test_host"],
        "User-Agent": "curl/8.0.0",
        "Accept": "*/*",
    }
    start = time.monotonic()
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=False,
            allow_redirects=False,
        ) as resp:
            await resp.read()
            latency = int((time.monotonic() - start) * 1000)
            cf_ray = resp.headers.get("cf-ray", "")
            return {
                "success": resp.status < 500 and bool(cf_ray),
                "status_code": resp.status,
                "latency": latency,
                "cf_ray": cf_ray,
                "country": resp.headers.get("cf-ipcountry", "unknown"),
            }
    except asyncio.TimeoutError:
        return {"success": False, "latency": -1, "error": "timeout"}
    except Exception as exc:
        return {"success": False, "latency": -1, "error": str(exc)}


async def check_one(
    session: aiohttp.ClientSession,
    target: str,
    semaphore: asyncio.Semaphore,
    countries: set[str],
    max_latency: int,
    timeout: int,
) -> dict[str, Any]:
    async with semaphore:
        tcp_ok, _ = await check_tcp(target, min(timeout, 3))
        if not tcp_ok:
            return {"ip": target, "valid": False, "reason": "TCP_FAIL", "latency": -1}

        result = await check_https(session, target, timeout)
        if not result["success"]:
            return {
                "ip": target,
                "valid": False,
                "reason": result.get("error", "HTTPS_FAIL"),
                "latency": result.get("latency", -1),
                "status_code": result.get("status_code"),
            }

        country = result.get("country", "unknown")
        if countries and country not in countries:
            return {"ip": target, "valid": False, "reason": f"WRONG_COUNTRY:{country}", "latency": result["latency"], "country": country}

        if result["latency"] > max_latency:
            return {"ip": target, "valid": False, "reason": "HIGH_LATENCY", "latency": result["latency"], "country": country}

        return {
            "ip": target,
            "valid": True,
            "latency": result["latency"],
            "country": country,
            "cf_ray": result.get("cf_ray", ""),
            "status_code": result.get("status_code"),
        }


async def check_all(targets: list[str], concurrency: int, countries: set[str], max_latency: int, timeout: int) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    completed = 0
    results: list[dict[str, Any]] = []

    async with aiohttp.ClientSession(connector=connector) as session:
        async def wrapped(target: str) -> dict[str, Any]:
            nonlocal completed
            result = await check_one(session, target, semaphore, countries, max_latency, timeout)
            completed += 1
            if sys.stdout.isatty() or completed % 10 == 0 or completed == len(targets):
                valid_count = sum(1 for item in results if item.get("valid")) + int(result.get("valid", False))
                print(f"\r  Progress: {completed}/{len(targets)} | valid: {valid_count}", end="", flush=True)
            return result

        for task in asyncio.as_completed([wrapped(target) for target in targets]):
            results.append(await task)

    print()
    return results


def format_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = sorted([item for item in results if item.get("valid")], key=lambda item: item["latency"])
    invalid = [item for item in results if not item.get("valid")]
    countries: dict[str, int] = {}
    failures: dict[str, int] = {}

    for item in valid:
        country = item.get("country", "unknown")
        countries[country] = countries.get(country, 0) + 1
    for item in invalid:
        reason = item.get("reason", "UNKNOWN")
        failures[reason] = failures.get(reason, 0) + 1

    avg_latency = round(sum(item["latency"] for item in valid) / len(valid)) if valid else 0
    return {
        "summary": {
            "total": len(results),
            "valid": len(valid),
            "invalid": len(invalid),
            "success_rate": f"{len(valid) / len(results) * 100:.1f}%" if results else "0%",
            "avg_latency_ms": avg_latency,
            "country_distribution": countries,
            "failure_reasons": failures,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "valid_ips": valid,
        "ip_list": [item["ip"] for item in valid],
    }


def write_publish_dir(result: dict[str, Any], publish_dir: str, best_count: int) -> None:
    out = Path(publish_dir)
    out.mkdir(parents=True, exist_ok=True)
    ips = result["ip_list"]
    us_ips = [item["ip"] for item in result["valid_ips"] if item.get("country") == "US"]

    (out / "all.txt").write_text("\n".join(ips), encoding="utf-8")
    (out / "us.txt").write_text("\n".join(us_ips), encoding="utf-8")
    (out / "best.txt").write_text("\n".join(ips[:best_count]), encoding="utf-8")
    (out / "full.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    encoded_v2ray = base64.b64encode("\n".join(ips).encode()).decode()
    (out / "v2ray.txt").write_text(encoded_v2ray, encoding="utf-8")
    (out / "index.html").write_text(render_status_html(result, best_count), encoding="utf-8")
    print(f"📦 Published static files to: {out.resolve()}")


def render_status_html(result: dict[str, Any], best_count: int) -> str:
    summary = result["summary"]
    rows = "\n".join(
        f"<tr><td>{i}</td><td><code>{item['ip']}</code></td><td>{item['latency']}ms</td><td>{item.get('country', '?')}</td></tr>"
        for i, item in enumerate(result["valid_ips"][:best_count], 1)
    )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CF ProxyIP Status</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:920px;margin:40px auto;padding:0 20px;line-height:1.5}}
.card{{display:inline-block;margin:6px 8px 6px 0;padding:14px 18px;border:1px solid #ddd;border-radius:12px;background:#fafafa}}
.card b{{display:block;font-size:26px}}table{{width:100%;border-collapse:collapse;margin-top:20px}}td,th{{border-bottom:1px solid #ddd;padding:10px;text-align:left}}code{{background:#f2f2f2;padding:2px 5px;border-radius:4px}}
</style>
</head>
<body>
<h1>CF ProxyIP Status</h1>
<p>Last checked: {summary['checked_at']}</p>
<div class="card">Valid<b>{summary['valid']}</b></div>
<div class="card">Total<b>{summary['total']}</b></div>
<div class="card">Success<b>{summary['success_rate']}</b></div>
<div class="card">Average latency<b>{summary['avg_latency_ms']}ms</b></div>
<h2>Files</h2>
<ul><li><code>all.txt</code></li><li><code>us.txt</code></li><li><code>best.txt</code></li><li><code>full.json</code></li><li><code>v2ray.txt</code></li></ul>
<h2>Best {best_count}</h2>
<table><thead><tr><th>#</th><th>IP / Domain</th><th>Latency</th><th>Country</th></tr></thead><tbody>{rows}</tbody></table>
</body>
</html>"""


def print_summary(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print("\n" + "=" * 52)
    print("CF ProxyIP check report")
    print("=" * 52)
    print(f"Total: {summary['total']} | valid: {summary['valid']} | invalid: {summary['invalid']}")
    print(f"Success: {summary['success_rate']} | avg latency: {summary['avg_latency_ms']}ms")
    print(f"Countries: {json.dumps(summary['country_distribution'], ensure_ascii=False)}")
    print(f"Failures: {json.dumps(summary['failure_reasons'], ensure_ascii=False)}")
    print("\nTop 10:")
    for i, item in enumerate(result["valid_ips"][:10], 1):
        print(f"  {i:2}. {item['ip']:<24} {item['latency']:>5}ms [{item.get('country', '?')}]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cloudflare ProxyIP candidates")
    parser.add_argument("--file", action="append", default=[], help="Input file with IP/domain candidates; can be used multiple times")
    parser.add_argument("--remote", action="store_true", help="Fetch built-in remote sources")
    parser.add_argument("--fofa", action="store_true", help="Fetch candidates from FOFA before checking them")
    parser.add_argument("--fofa-query", help="FOFA query; defaults to a US Cloudflare HTTPS query")
    parser.add_argument("--fofa-page", type=int, default=1)
    parser.add_argument("--fofa-size", type=int, default=100)
    parser.add_argument("--fofa-fields", default="ip,host,port")
    parser.add_argument("--fofa-full", action="store_true", help="Ask FOFA to search all historical data")
    parser.add_argument("--fofa-key", help="FOFA API key; prefer FOFA_KEY environment variable")
    parser.add_argument("--include-domains", action="store_true", help="Allow domain candidates. Default is IPv4-only output")
    parser.add_argument("--output", help="Write full JSON result")
    parser.add_argument("--publish-dir", help="Write static distribution files")
    parser.add_argument("--concurrency", type=int, default=CONFIG["concurrency"])
    parser.add_argument("--timeout", type=int, default=CONFIG["timeout"], help="Per-target timeout in seconds")
    parser.add_argument("--max-latency", type=int, default=CONFIG["max_latency"], help="Maximum accepted latency in ms")
    parser.add_argument("--country", default="US", help="Comma-separated country codes; ignored with --no-country-filter")
    parser.add_argument("--no-country-filter", action="store_true")
    parser.add_argument("--best-count", type=int, default=10)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    countries = set() if args.no_country_filter else {item.strip().upper() for item in args.country.split(",") if item.strip()}
    print(f"🚀 concurrency={args.concurrency} timeout={args.timeout}s max_latency={args.max_latency}ms countries={sorted(countries) or 'ANY'}")

    targets = await collect_targets(
        args.file,
        args.remote,
        args.fofa,
        args.fofa_query,
        args.fofa_page,
        args.fofa_size,
        args.fofa_fields,
        args.fofa_full,
        args.fofa_key,
        args.country,
        not args.include_domains,
    )
    print(f"📋 Candidates: {len(targets)}")
    if not targets:
        raise SystemExit("No valid IP/domain candidates found")

    raw_results = await check_all(targets, args.concurrency, countries, args.max_latency, args.timeout)
    result = format_results(raw_results)
    print_summary(result)

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 Wrote JSON: {Path(args.output).resolve()}")
    if args.publish_dir:
        write_publish_dir(result, args.publish_dir, args.best_count)

    if not args.output and not args.publish_dir:
        output = Path(f"valid_ips_{int(time.time())}.txt")
        output.write_text("\n".join(result["ip_list"]), encoding="utf-8")
        print(f"💾 Wrote IP list: {output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
