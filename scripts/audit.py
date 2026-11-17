#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from _common import LIST_DOMAIN, PROXY_DOMAIN, fetch, hmac_token, kv_namespace_id, wrangler_cmd

HEALTH_RETRY_ATTEMPTS = 10
HEALTH_RETRY_DELAY_S = 10
TARGET_COUNTRIES = {"US"}


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: str) -> dict | list:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dns_ips() -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(PROXY_DOMAIN, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)})


def kv_get(key: str) -> str:
    return subprocess.check_output([
        wrangler_cmd(), "kv", "key", "get", key,
        "--namespace-id", kv_namespace_id(),
        "--remote",
    ], text=True).strip()


def main() -> None:
    token = hmac_token()
    current = Path("docs/current.txt").read_text(encoding="utf-8").strip()
    current_json = read_json("docs/current.json")
    state = read_json("docs/state.json")
    full = read_json("docs/full.json")
    dns_records = read_json("docs/dns-records.json")

    # Local file consistency checks
    assert_true(bool(current), "docs/current.txt is empty")
    assert_true(state.get("current_ip") == current, "state current_ip does not match current.txt")
    assert_true(current_json.get("current", {}).get("ip") == current, "current.json does not match current.txt")
    assert_true(full.get("summary", {}).get("current_ip") == current, "full.json summary current_ip does not match current.txt")
    assert_true(len(dns_records) == 1 and dns_records[0].get("content") == current, "dns-records.json must contain exactly one current IP")

    country = (current_json.get("current", {}).get("risk", {}).get("country") or "").upper()
    degraded = state.get("status") == "degraded" and int(state.get("failure_count") or 0) < int(state.get("failover_threshold") or 2)
    if degraded and not country:
        # 主 IP 本轮验证失败但未达 failover 阈值：允许通过，让 failure_count 被提交，下一轮才能真正切换。
        print(f"WARN: current IP is degraded (failure_count={state.get('failure_count')}); country check skipped", flush=True)
    else:
        assert_true(country in TARGET_COUNTRIES, f"current IP country {country!r} is outside target countries {sorted(TARGET_COUNTRIES)}")

    standby = full.get("standby", [])
    bad_standby = [(x.get("ip"), x.get("risk", {}).get("country")) for x in standby if (x.get("risk", {}).get("country") or "").upper() not in TARGET_COUNTRIES]
    assert_true(not bad_standby, f"standby contains non-target countries: {bad_standby[:5]}")

    # DNS resolution check
    resolved = dns_ips()
    assert_true(resolved == [current], f"DNS must resolve to exactly current IP: {resolved} != {[current]}")

    # Live endpoint checks (using HMAC token)
    status, body = fetch(f"{LIST_DOMAIN}/current.txt?r={int(time.time())}", token)
    assert_true(status == 200 and body.strip() == current, f"live current.txt mismatch: {status} {body[:120]}")

    # Worker isolate 会把 result_json 缓存 60 秒（RESULT_CACHE_TTL_MS），刚同步完 KV 时可能读到旧值；
    # 最多重试约 90 秒，仍不一致才判失败。
    health: dict = {}
    status, body = 0, ""
    for attempt in range(1, HEALTH_RETRY_ATTEMPTS + 1):
        status, body = fetch(f"{LIST_DOMAIN}/health/full?r={int(time.time())}", token)
        if status == 200:
            try:
                health = json.loads(body)
            except json.JSONDecodeError:
                health = {}
            if health.get("current") == current:
                break
        if attempt < HEALTH_RETRY_ATTEMPTS:
            print(f"  health/full attempt {attempt}/{HEALTH_RETRY_ATTEMPTS}: status={status} current={health.get('current')}, want {current}", flush=True)
            time.sleep(HEALTH_RETRY_DELAY_S)
    assert_true(status == 200, f"live health failed: {status} {body[:120]}")
    assert_true(health.get("current") == current, f"live health current mismatch: {health.get('current')} != {current}")

    # Token rejection check: old YYYYMMDD token should now be rejected
    old_token = time.strftime("%Y%m%d", time.gmtime())
    status_bad, _ = fetch(f"{LIST_DOMAIN}/all.txt?t={old_token}&r={int(time.time())}")
    assert_true(status_bad == 403, f"Old YYYYMMDD token should be rejected but got HTTP {status_bad}")

    # 无 cookie/token 的直连应被拦（防爬减速带是否生效；不代表数据保密，首页会给任何访客发 cookie）
    status_noauth, _ = fetch(f"{LIST_DOMAIN}/all.txt?r={int(time.time())}")
    assert_true(status_noauth == 403, f"Unauthenticated request should be rejected but got HTTP {status_noauth}")

    # KV consistency check
    try:
        kv_current = kv_get("current_txt")
        assert_true(kv_current == current, f"KV current_txt mismatch: {kv_current} != {current}")
    except Exception as exc:
        raise RuntimeError(f"KV audit failed: {exc}") from exc

    print(json.dumps({
        "ok": True,
        "current": current,
        "country": country,
        "dns": resolved,
        "standby_count": len(standby),
        "health_count": health.get("count"),
        "token": "hmac",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"AUDIT FAILED: {exc}", file=sys.stderr)
        raise
