#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

TOKEN = time.strftime("%Y%m%d", time.gmtime())
LIST_DOMAIN = "https://list.leilaomi.cc.cd"
PROXY_DOMAIN = "proxyip.leilaomi.cc.cd"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, check=check)


def fetch(url: str) -> tuple[int, str]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
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
    return None


def git_dirty() -> bool:
    out = subprocess.check_output(["git", "status", "--short"], text=True, stderr=subprocess.DEVNULL)
    return bool(out.strip())


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


def verify_live() -> None:
    """Wait for Cloudflare global sync then verify live endpoints."""
    print("⏳ Waiting 30s for Cloudflare global sync...", flush=True)
    time.sleep(30)

    primary = current_primary()
    if not primary:
        print("⚠️  No primary IP, skipping live verification", flush=True)
        return

    expected = [primary]
    # Wait up to 20 attempts × 10s for current.txt to match
    for attempt in range(1, 21):
        status, body = fetch(f"{LIST_DOMAIN}/current.txt?t={TOKEN}&r={int(time.time())}")
        if status == 200:
            live = [x.strip() for x in body.splitlines() if x.strip()]
            if live == expected:
                print(f"✅ Live current.txt matches: {live}", flush=True)
                break
            print(f"  attempt {attempt}/20: got {live}, want {expected}", flush=True)
        else:
            print(f"  attempt {attempt}/20: HTTP {status}", flush=True)
        if attempt < 20:
            time.sleep(10)
    else:
        print("⚠️  Live current.txt verification timed out, DNS will sync in background", flush=True)

    # Verify health endpoint
    status, body = fetch(f"{LIST_DOMAIN}/health?t={TOKEN}&r={int(time.time())}")
    if status == 200:
        health = json.loads(body)
        print(f"✅ Health: {json.dumps(health)}", flush=True)
    else:
        print(f"⚠️  Health endpoint not ready: {status}", flush=True)


def main() -> None:
    before = current_primary()

    # Step 1: Generate fresh data
    run([sys.executable, "build_dataset.py"])
    validate_outputs()

    after = current_primary()

    # Step 2: Sync to KV + DNS + Deploy Worker
    run([sys.executable, "scripts/sync_kv.py"])
    run([sys.executable, "scripts/sync_dns.py"])
    run(["wrangler", "deploy"])

    # Step 3: Verify
    verify_live()
    run([sys.executable, "scripts/audit.py"])

    # Step 4: Git commit if changed
    changed = before != after or git_dirty()
    if changed:
        run(["git", "config", "user.name", "github-actions[bot]"])
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
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
