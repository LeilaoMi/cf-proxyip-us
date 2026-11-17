#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _common import LIST_DOMAIN, PROXY_DOMAIN, fetch, hmac_token, wrangler_cmd

PUSH_ATTEMPTS = 3
DATA_FILES = [
    "docs/all.txt", "docs/base64.txt", "docs/best.txt", "docs/current.json", "docs/current.txt",
    "docs/dns-records.json", "docs/full.json", "docs/history.json", "docs/ip_history.json",
    "docs/kv-manifest.json", "docs/standby.txt", "docs/state.json", "docs/top5.txt", "docs/us.txt",
    "docs/v2ray.txt",
]


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, check=check)


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
        status, body = fetch(f"{LIST_DOMAIN}/current.txt?r={int(time.time())}", token, timeout=45)
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
    status, body = fetch(f"{LIST_DOMAIN}/health/full?r={int(time.time())}", token, timeout=45)
    if status == 200:
        print(f"✅ Health: {body.strip()[:500]}", flush=True)
    else:
        print(f"⚠️  Health endpoint not ready: {status}", flush=True)


def commit_and_push(before: str | None, after: str | None) -> None:
    if not (before != after or git_dirty()):
        return
    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "add", *DATA_FILES])
    valid_count = len([x for x in Path("docs/all.txt").read_text().splitlines() if x.strip()])
    commit = run(["git", "commit", "-m", f"Auto refresh ProxyIP data: current={after} valid={valid_count}"], check=False)
    if commit.returncode != 0:
        print("No commit created; skipping push", flush=True)
        return
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        pulled = run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=False)
        if pulled.returncode == 0 and run(["git", "push"], check=False).returncode == 0:
            return
        if pulled.returncode != 0:
            run(["git", "rebase", "--abort"], check=False)
        print(f"push attempt {attempt}/{PUSH_ATTEMPTS} failed", flush=True)
        if attempt < PUSH_ATTEMPTS:
            time.sleep(5 * attempt)
    raise SystemExit(
        f"LIVE DATA UPDATED (current={after}) BUT STATE WAS NOT PUSHED; "
        "fix the repo and rerun, otherwise the next round starts from stale state.json"
    )


def main() -> None:
    before = current_primary()

    # Step 1: Generate fresh data
    if os.environ.get("PROXYIP_SKIP_GENERATE") == "1":
        print("Skipping build_dataset.py because PROXYIP_SKIP_GENERATE=1", flush=True)
    else:
        run([sys.executable, "build_dataset.py"])
    run([sys.executable, "scripts/validate_outputs.py"])

    after = current_primary()

    # Step 2: Deploy Worker first, then sync KV + DNS.
    # 先部署：wrangler/Node 等环境问题在写线上数据之前就暴露，避免线上已更新但 state 未提交。
    run([wrangler_cmd(), "deploy"])
    run([sys.executable, "scripts/sync_kv.py"])
    run([sys.executable, "scripts/sync_dns.py"])

    # Step 3: 线上已经切换，立刻提交 state，再做验证/审计。
    # 以前是验证、审计都通过后才提交：任何一步失败，线上已切但 state.json 没进仓库，
    # 下一轮从旧状态出发，failure_count / last_switch_at 全错，可能重复切换或绕过冷却。
    commit_and_push(before, after)

    # Step 4: Verify
    verify_live()
    run([sys.executable, "scripts/audit.py"])

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
