#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from _common import kv_namespace_id


def get_kv_namespace_id() -> str:
    return kv_namespace_id()


def get_account_id() -> str:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account_id:
        raise RuntimeError("Missing CLOUDFLARE_ACCOUNT_ID environment variable")
    return account_id


def get_api_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing CLOUDFLARE_API_TOKEN environment variable")
    return token


def put_kv_value(account_id: str, namespace_id: str, token: str, key: str, path: Path) -> None:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/storage/kv/namespaces/{namespace_id}/values/{urllib.request.quote(key, safe='')}"
    )
    data = path.read_bytes()
    print(f"PUT KV {key} <- {path} ({len(data)} bytes)", flush=True)
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = res.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Cloudflare KV PUT failed for {key}: HTTP {exc.code} {body}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cloudflare KV PUT returned non-JSON for {key}: {body[:500]}") from exc
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare KV PUT failed for {key}: {json.dumps(payload, ensure_ascii=False)}")


def main() -> None:
    account_id = get_account_id()
    token = get_api_token()
    namespace_id = get_kv_namespace_id()
    manifest = json.loads(Path("docs/kv-manifest.json").read_text(encoding="utf-8"))

    for key, source in manifest.items():
        path = Path(source)
        if not path.exists():
            raise RuntimeError(f"Missing KV source file: {path}")
        # docs/full.json 生成时已剔除 all_results / throughput_by_ip，直接上传即可
        put_kv_value(account_id, namespace_id, token, key, path)

    print(json.dumps({"synced_keys": sorted(manifest), "namespace_id": namespace_id, "account_id": account_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()
