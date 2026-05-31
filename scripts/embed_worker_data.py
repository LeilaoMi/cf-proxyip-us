#!/usr/bin/env python3
from __future__ import annotations
import json
import re
from pathlib import Path

FULL_JSON = Path("docs/full.json")
CURRENT_JSON = Path("docs/current.json")
STATE_JSON = Path("docs/state.json")
HISTORY_JSON = Path("docs/history.json")
WORKER_JS = Path("worker.js")
KV_MANIFEST = Path("docs/kv-manifest.json")


def slim_item(item: dict) -> dict:
    risk = item.get("risk") or {}
    return {
        "ip": item.get("ip"),
        "latency_ms": item.get("latency_ms"),
        "portRemote": item.get("portRemote", 443),
        "colo": risk.get("colo") or item.get("colo"),
        "risk": {
            "cf_bot_score": risk.get("cf_bot_score"),
            "grade": risk.get("grade"),
            "corporate_proxy": risk.get("corporate_proxy"),
            "verified_bot": risk.get("verified_bot"),
            "asn": risk.get("asn"),
            "as_organization": risk.get("as_organization"),
            "country": risk.get("country"),
            "city": risk.get("city"),
        },
    }


def main() -> None:
    full = json.loads(FULL_JSON.read_text(encoding="utf-8"))
    current_doc = json.loads(CURRENT_JSON.read_text(encoding="utf-8")) if CURRENT_JSON.exists() else {}
    state = json.loads(STATE_JSON.read_text(encoding="utf-8")) if STATE_JSON.exists() else {}
    history = json.loads(HISTORY_JSON.read_text(encoding="utf-8")) if HISTORY_JSON.exists() else []
    slim = {
        "summary": full["summary"],
        "current": current_doc.get("current"),
        "state": state,
        "history": history[-50:],
        "standby": [slim_item(x) for x in full.get("standby", [])],
        "recommended_top5": [slim_item(x) for x in full.get("recommended_top5", [])],
        "valid_ips": [slim_item(x) for x in full.get("valid_ips", [])],
    }
    content = WORKER_JS.read_text(encoding="utf-8")
    embedded = "const DEFAULT_RESULT = " + json.dumps(slim, ensure_ascii=False, separators=(",", ":")) + ";\n\nconst ACCESS_COOKIE"
    updated = re.sub(r"^const DEFAULT_RESULT = .*?;\n\nconst ACCESS_COOKIE", embedded, content, flags=re.S)
    manifest = {
        "result_json": "docs/full.json",
        "current_json": "docs/current.json",
        "current_txt": "docs/current.txt",
        "standby_txt": "docs/standby.txt",
        "history_json": "docs/history.json",
        "state_json": "docs/state.json",
    }
    KV_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if updated != content:
        WORKER_JS.write_text(updated, encoding="utf-8")
    print(json.dumps({
        "embedded_current": (slim.get("current") or {}).get("ip"),
        "embedded_standby": [x["ip"] for x in slim["standby"][:5]],
        "embedded_valid": len(slim["valid_ips"]),
        "embedded_top5": [x["ip"] for x in slim["recommended_top5"]],
        "worker_updated": updated != content,
        "kv_manifest": str(KV_MANIFEST),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
