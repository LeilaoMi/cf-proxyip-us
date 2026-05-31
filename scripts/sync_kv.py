#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

MANIFEST = Path("docs/kv-manifest.json")
NAMESPACE_ID = "6d911271a65f4e67a39e22d991edb961"


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key, source in manifest.items():
        path = Path(source)
        if not path.exists():
            raise RuntimeError(f"Missing KV source file: {path}")
        run(["wrangler", "kv", "key", "put", key, "--path", str(path), "--namespace-id", NAMESPACE_ID, "--remote"])
    print(json.dumps({"synced_keys": sorted(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
