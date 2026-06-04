from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "worker.js").read_text(encoding="utf-8")


class WorkerStaticTest(unittest.TestCase):
    def test_token_uses_no_store_and_no_default_legacy(self) -> None:
        self.assertIn("private, no-store", WORKER)
        self.assertIn("ALLOW_LEGACY_DATE_TOKEN", WORKER)
        self.assertIn("PROXYIP_SECRET is not configured", WORKER)
        self.assertNotIn("return reply(json({ token: today", WORKER)

    def test_bearer_auth_and_cors_are_scoped(self) -> None:
        self.assertIn("Authorization: Bearer TOKEN", WORKER)
        self.assertIn("function bearerToken", WORKER)
        self.assertIn("if (allowCors) headers.set(\"access-control-allow-origin\", \"*\")", WORKER)
        bare_cors_lines = [line for line in WORKER.splitlines() if line.strip() == 'headers.set("access-control-allow-origin", "*");']
        self.assertEqual(bare_cors_lines, [])

    def test_lightweight_kv_paths_are_configured(self) -> None:
        for key in ["current_txt", "standby_txt", "all_txt", "top5_txt", "base64_txt", "v2ray_txt"]:
            self.assertIn(key, WORKER)
        self.assertIn("RESULT_CACHE_TTL_MS", WORKER)
        self.assertIn("loadTextKey", WORKER)


if __name__ == "__main__":
    unittest.main()
