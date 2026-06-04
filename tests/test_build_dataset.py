from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dataset", ROOT / "build_dataset.py")
build_dataset = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(build_dataset)


class BuildDatasetLogicTest(unittest.TestCase):
    def test_direct_https_fallback_is_down_ranked(self) -> None:
        cmliu = build_dataset.enrich({
            "ip": "1.1.1.1",
            "success": True,
            "supports_ipv4": True,
            "latency_ms": 100,
            "probe_results": {"ipv4": {"exit": {
                "country": "US",
                "colo": "IAD",
                "asn": 1,
                "botManagement": {"score": 95, "corporateProxy": False, "verifiedBot": False},
            }}},
        }, {"sources": ["cmliu"]})
        fallback = build_dataset.enrich({
            "ip": "2.2.2.2",
            "success": True,
            "supports_ipv4": True,
            "latency_ms": 100,
            "country": "US",
            "cf_bot_score": 95,
            "method": "direct_https",
            "fallback_unverified": True,
        }, {"sources": ["fallback"]})

        self.assertEqual(fallback["risk"]["grade"], "fallback_unverified")
        self.assertEqual(fallback["risk"]["verification_method"], "direct_https")
        self.assertEqual(cmliu["risk"]["verification_method"], "cmliu")
        self.assertLess(build_dataset.rank_key(cmliu), build_dataset.rank_key(fallback))
        self.assertGreater(cmliu["stable_score"], fallback["stable_score"])

    def test_diverse_candidates_limits_asn(self) -> None:
        current = {"ip": "10.0.0.1", "risk": {"asn": 1}}
        items = [
            {"ip": "10.0.0.2", "risk": {"asn": 1}},
            {"ip": "10.0.0.3", "risk": {"asn": 2}},
            {"ip": "10.0.0.4", "risk": {"asn": 2}},
            {"ip": "10.0.0.5", "risk": {"asn": 3}},
        ]

        top = build_dataset.diverse_candidates(items, current, 4, 1)
        self.assertEqual([x["ip"] for x in top], ["10.0.0.3", "10.0.0.5"])

        standby = build_dataset.diverse_candidates(items, current, 4, 2)
        self.assertEqual([x["ip"] for x in standby], ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"])


if __name__ == "__main__":
    unittest.main()
