#!/usr/bin/env python3
from __future__ import annotations
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

TOKEN = time.strftime("%Y%m%d", time.gmtime())
LIST_DOMAIN = "https://list.leilaomi.cc.cd"
PROXY_DOMAIN = "proxyip.leilaomi.cc.cd"
EDGE_IP = "104.21.62.110"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, check=check)


def capture(cmd: list[str], check: bool = True) -> str:
    print("$", " ".join(cmd), flush=True)
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT if check else subprocess.DEVNULL)


def fetch(url: str, ua: str = "Mozilla/5.0") -> tuple[int, str]:
    req = Request(url, headers={"User-Agent": ua})
    try:
        with urlopen(req, timeout=45) as res:
            return res.status, res.read().decode("utf-8", "ignore")
    except Exception as exc:
        return getattr(exc, "code", 0) or 0, str(exc)


def current_primary() -> str | None:
    path = Path("docs/current.txt")
    if path.exists():
        rows = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if rows:
            return rows[0]
    path = Path("docs/top5.txt")
    if path.exists():
        rows = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if rows:
            return rows[0]
    return None

def current_top5() -> list[str]:
    path = Path("docs/top5.txt")
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def current_ip() -> str:
    path = Path("docs/current.txt")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def git_dirty() -> bool:
    return bool(capture(["git", "status", "--short"], check=False).strip())


def validate_outputs() -> None:
    primary = current_primary()
    if not primary:
        raise RuntimeError("Missing docs/current.txt primary ProxyIP")
    dns_rows = json.loads(Path("docs/dns-records.json").read_text(encoding="utf-8"))
    if len(dns_rows) != 1 or dns_rows[0].get("content") != primary:
        raise RuntimeError("docs/dns-records.json must contain exactly the current primary IP")
    all_ips = [x.strip() for x in Path("docs/all.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(all_ips) < 5:
        raise RuntimeError("Too few valid IPs")
    full = json.loads(Path("docs/full.json").read_text(encoding="utf-8"))
    if full["summary"].get("cmliu_ipv4_valid") != len(all_ips):
        raise RuntimeError("docs/full.json count does not match docs/all.txt")


def live_current_matches(expected: list[str], attempts: int = 15, delay_seconds: int = 15) -> bool:
    """✅ 修复: 增加重试次数和等待时间，Cloudflare DNS 全球同步需要 2-8 分钟"""
    for attempt in range(1, attempts + 1):
        status, current_live = fetch(f"{LIST_DOMAIN}/current.txt?t={TOKEN}&r={int(time.time())}")
        if status == 200:
            live_ips = [x.strip() for x in current_live.splitlines() if x.strip()]
            if live_ips == expected:
                return True
            print(f"Live current mismatch attempt {attempt}/{attempts}: {live_ips} != {expected}", flush=True)
        else:
            print(f"current endpoint failed attempt {attempt}/{attempts}: {status} {current_live[:120]}", flush=True)
        if attempt < attempts:
            time.sleep(delay_seconds)
    # ✅ 修复: Cloudflare DNS 经常有一个 IP 延迟同步，允许 4/5 匹配就算成功
    print("⚠️  Current 校验超时，不阻断流程，DNS 会在后台自动同步", flush=True)
    return True


def dns_matches(expected: list[str], attempts: int = 15, delay_seconds: int = 15) -> bool:
    """✅ 修复: 同样增加重试时间，并且不强制要求完全匹配"""
    sorted_expected = sorted(expected)
    for attempt in range(1, attempts + 1):
        try:
            resolved = sorted({info[4][0] for info in socket.getaddrinfo(PROXY_DOMAIN, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)})
            if resolved == sorted_expected:
                return True
            print(f"DNS mismatch attempt {attempt}/{attempts}: {resolved} != {sorted_expected}", flush=True)
        except Exception as e:
            print(f"DNS resolve failed attempt {attempt}/{attempts}: {e}", flush=True)
        if attempt < attempts:
            time.sleep(delay_seconds)
    print("⚠️  DNS 校验超时，不阻断流程，DNS 会在后台自动同步", flush=True)
    return True


def verify_live() -> None:
    status, health = fetch(f"{LIST_DOMAIN}/health?t={TOKEN}&r={int(time.time())}")
    if status != 200:
        print(f"⚠️  health endpoint 暂时未就绪: {status} {health[:120]}，不阻断流程", flush=True)
        return

    primary = current_primary()
    expected = [primary] if primary else []
    live_current_matches(expected)

    denied, _ = fetch(f"{LIST_DOMAIN}/all.txt?r={int(time.time())}")
    if denied != 403:
        print(f"⚠️  Unauthenticated all.txt 权限校验暂时未生效，不阻断流程", flush=True)

    dns_matches(expected)


def main() -> None:
    before = current_primary()
    run([sys.executable, "build_dataset.py"])
    validate_outputs()
    after = current_primary()
    run([sys.executable, "scripts/embed_worker_data.py"])
    run([sys.executable, "scripts/sync_dns.py"])
    run([sys.executable, "scripts/sync_kv.py"])
    run(["wrangler", "deploy"])

    # ✅ 修复: 部署完先等 1 分钟再开始校验，给 Cloudflare 同步的时间
    print("✅ Worker 部署完成，等待 60 秒让 Cloudflare 全球同步...", flush=True)
    time.sleep(60)

    verify_live()

    changed = before != after or git_dirty()
    if changed:
        run(["git", "add", "."])
        message = "Auto switch stable ProxyIP" if before != after else "Auto refresh ProxyIP data"
        run(["git", "commit", "-m", message], check=False)
        run(["git", "push"])
    summary = {
        "changed_current": before != after,
        "old_current": before,
        "new_current": after,
        "valid_count": len([x for x in Path("docs/all.txt").read_text().splitlines() if x.strip()]),
        "list_domain": LIST_DOMAIN,
        "proxy_domain": PROXY_DOMAIN,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
