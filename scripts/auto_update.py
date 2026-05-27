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


def current_top5() -> list[str]:
    path = Path("docs/top5.txt")
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def git_dirty() -> bool:
    return bool(capture(["git", "status", "--short"], check=False).strip())


def validate_outputs() -> None:
    top5 = current_top5()
    if len(top5) != 5:
        raise RuntimeError(f"Expected 5 top5 IPs, got {len(top5)}")
    all_ips = [x.strip() for x in Path("docs/all.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(all_ips) < 5:
        raise RuntimeError("Too few valid IPs")
    full = json.loads(Path("docs/full.json").read_text(encoding="utf-8"))
    if full["summary"].get("cmliu_ipv4_valid") != len(all_ips):
        raise RuntimeError("docs/full.json count does not match docs/all.txt")


def verify_live() -> None:
    status, health = fetch(f"{LIST_DOMAIN}/health?t={TOKEN}&r={int(time.time())}")
    if status != 200:
        raise RuntimeError(f"health endpoint failed: {status} {health[:120]}")
    status, top5_live = fetch(f"{LIST_DOMAIN}/top5.txt?t={TOKEN}&r={int(time.time())}")
    if status != 200:
        raise RuntimeError(f"top5 endpoint failed: {status} {top5_live[:120]}")
    live_ips = [x.strip() for x in top5_live.splitlines() if x.strip()]
    if live_ips != current_top5():
        raise RuntimeError(f"Live top5 mismatch: {live_ips} != {current_top5()}")
    denied, _ = fetch(f"{LIST_DOMAIN}/all.txt?r={int(time.time())}")
    if denied != 403:
        raise RuntimeError(f"Unauthenticated all.txt should be 403, got {denied}")
    bot, _ = fetch(f"{LIST_DOMAIN}/all.txt?t={TOKEN}&r={int(time.time())}", ua="curl/8.0")
    if bot != 403:
        raise RuntimeError(f"curl UA should be 403, got {bot}")
    resolved = sorted({info[4][0] for info in socket.getaddrinfo(PROXY_DOMAIN, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)})
    expected = sorted(current_top5())
    if resolved != expected:
        raise RuntimeError(f"DNS mismatch: {resolved} != {expected}")


def main() -> None:
    before = current_top5()
    run([sys.executable, "build_dataset.py"])
    validate_outputs()
    after = current_top5()
    run([sys.executable, "scripts/embed_worker_data.py"])
    run([sys.executable, "scripts/sync_dns.py"])
    run(["wrangler", "deploy"])
    verify_live()

    changed = before != after or git_dirty()
    if changed:
        run(["git", "add", "."])
        message = "Auto update ProxyIP top5" if before != after else "Auto refresh ProxyIP data"
        run(["git", "commit", "-m", message], check=False)
        run(["git", "push"])
    summary = {
        "changed_top5": before != after,
        "old_top5": before,
        "new_top5": after,
        "valid_count": len([x for x in Path("docs/all.txt").read_text().splitlines() if x.strip()]),
        "list_domain": LIST_DOMAIN,
        "proxy_domain": PROXY_DOMAIN,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
