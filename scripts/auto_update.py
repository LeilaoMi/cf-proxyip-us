#!/usr/bin/env python3
from __future__ import annotations
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

def hmac_token() -> str:
    secret = os.environ.get("PROXYIP_HMAC_SECRET", "")
    if secret:
        date_str = time.strftime("%Y%m%d", time.gmtime())
        return f"{date_str}-{hmac.new(secret.encode(), date_str.encode(), hashlib.sha256).hexdigest()}"

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.open(Request(f"{LIST_DOMAIN}/", headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
    with opener.open(Request(f"{LIST_DOMAIN}/token", headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as res:
        data = json.loads(res.read().decode("utf-8", "ignore"))
    token = data.get("token")
    if not token:
        raise SystemExit("Cannot obtain HMAC token from /token")
    return token

LIST_DOMAIN = os.environ.get("PROXYIP_LIST_DOMAIN", "https://list.leilaomi.cc.cd").rstrip("/")
PROXY_DOMAIN = os.environ.get("PROXYIP_RECORD_NAME", "proxyip.leilaomi.cc.cd")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, check=check)


def fetch(url: str, token: str | None = None) -> tuple[int, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
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


def verify_live() -> None:
    """Wait for Cloudflare global sync then verify live endpoints."""
    print("⏳ Waiting 30s for Cloudflare global sync...", flush=True)
    time.sleep(30)

    primary = current_primary()
    if not primary:
        print("⚠️  No primary IP, skipping live verification", flush=True)
        return

    token = hmac_token()
    expected = [primary]
    # Wait up to 20 attempts × 10s for current.txt to match
    for attempt in range(1, 21):
        status, body = fetch(f"{LIST_DOMAIN}/current.txt?r={int(time.time())}", token)
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
    status, body = fetch(f"{LIST_DOMAIN}/health/full?r={int(time.time())}", token)
    if status == 200:
        health = json.loads(body)
        print(f"✅ Health: {json.dumps(health)}", flush=True)
    else:
        print(f"⚠️  Health endpoint not ready: {status}", flush=True)


def main() -> None:
    before = current_primary()

    # Step 1: Generate fresh data
    if os.environ.get("PROXYIP_SKIP_GENERATE") == "1":
        print("Skipping build_dataset.py because PROXYIP_SKIP_GENERATE=1", flush=True)
    else:
        run([sys.executable, "build_dataset.py"])
    run([sys.executable, "scripts/validate_outputs.py"])

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
        run(["git", "add", "docs/all.txt", "docs/base64.txt", "docs/best.txt", "docs/current.json", "docs/current.txt", "docs/dns-records.json", "docs/full.json", "docs/history.json", "docs/kv-manifest.json", "docs/standby.txt", "docs/state.json", "docs/top5.txt", "docs/us.txt", "docs/v2ray.txt"])
        valid_count = len([x for x in Path("docs/all.txt").read_text().splitlines() if x.strip()])
        message = f"Auto refresh ProxyIP data: current={after} valid={valid_count}"
        commit = run(["git", "commit", "-m", message], check=False)
        if commit.returncode == 0:
            run(["git", "pull", "--rebase", "--autostash", "origin", "main"])
            run(["git", "push"])
        else:
            print("No commit created; skipping push", flush=True)

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
