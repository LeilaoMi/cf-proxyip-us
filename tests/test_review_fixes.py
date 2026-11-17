"""2026-11 代码审查修复的回归测试。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import _common  # noqa: E402
import auto_update  # noqa: E402

import build_dataset as bd  # noqa: E402

WORKER = (ROOT / "worker.js").read_text(encoding="utf-8")


def item(ip: str, latency: int = 100, bot: int = 95, country: str = "US", mbps: float | None = 5.0) -> dict:
    x = {
        "ip": ip, "success": True, "supports_ipv4": True,
        "latency_ms": latency, "rtt_p50_ms": latency, "rtt_jitter_ms": 0, "rtt_samples": 3,
        "rtt_ok": True, "probe_attempted": True, "eff_throughput_mbps": mbps,
        "probe_results": {"ipv4": {"exit": {"country": country, "colo": "IAD", "asn": int(ip.split(".")[-1]),
                                             "botManagement": {"score": bot, "corporateProxy": False, "verifiedBot": False}}}},
    }
    return bd.enrich(x, {"sources": ["s"]}, bd.default_stability())


class ThroughputAbortTest(unittest.TestCase):
    def test_slow_pipe_aborts_after_grace(self) -> None:
        # 4 秒只收到 200KB = 0.05 MB/s，低于 0.4 MB/s，应放弃
        self.assertTrue(bd.throughput_too_slow(200_000, 4.0))

    def test_fast_pipe_and_grace_period_continue(self) -> None:
        self.assertFalse(bd.throughput_too_slow(8_000_000, 4.0))
        self.assertFalse(bd.throughput_too_slow(0, 1.0))


class QualitySwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        docs = Path(tmp.name)
        for name, value in (("DOCS", docs), ("STATE_PATH", docs / "state.json"),
                            ("CURRENT_PATH", docs / "current.txt"), ("HISTORY_PATH", docs / "history.json")):
            patcher = mock.patch.object(bd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (docs / "state.json").write_text(json.dumps({
            "current_ip": "9.9.9.9", "status": "healthy", "failure_count": 0,
            "last_switch_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        (docs / "current.txt").write_text("9.9.9.9\n", encoding="utf-8")
        (docs / "history.json").write_text("[]", encoding="utf-8")

    def test_switches_immediately_when_better_candidate_passes_gate(self) -> None:
        slow = item("9.9.9.9", latency=4000)  # 超过 2500ms 主 IP 门槛
        good = item("10.0.0.2", latency=120)
        current, state, history = bd.select_current([good, slow], [])
        self.assertEqual(current["ip"], "10.0.0.2")
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(history[0]["reason"], "quality_below_threshold")

    def test_does_not_switch_to_slower_throughput_candidate(self) -> None:
        cur = item("9.9.9.9", latency=100, mbps=0.5)  # 吞吐低于 1 MB/s 主 IP 门槛
        slower = item("10.0.0.7", latency=90, mbps=0.1)
        current, state, _ = bd.select_current([slower, cur], [])
        self.assertEqual(current["ip"], "9.9.9.9")
        self.assertEqual(state["status"], "degraded_quality_no_better")

    def test_keeps_current_when_no_candidate_passes_gate(self) -> None:
        slow = item("9.9.9.9", latency=4000)
        worse = item("10.0.0.3", latency=5000)
        current, state, history = bd.select_current([slow, worse], [])
        self.assertEqual(current["ip"], "9.9.9.9")
        self.assertEqual(state["status"], "degraded_quality_no_better")
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(history, [])


class FallbackVerificationTest(unittest.TestCase):
    def test_direct_https_without_loc_is_not_target_region(self) -> None:
        x = {"ip": "10.0.0.4", "success": True, "supports_ipv4": True, "latency_ms": 100,
             "country": None, "cf_bot_score": None, "method": "direct_https", "fallback_unverified": True}
        bd.enrich(x, {"sources": ["s"], "country_hint": "US"})
        self.assertIsNone(x["risk"]["cf_bot_score"])
        self.assertIsNone(x["risk"]["country"])  # 来源提示不能冒充已验证地区
        self.assertFalse(bd.is_target_region(x))


class CandidateTest(unittest.TestCase):
    def test_cloudflare_ranges_fall_back_to_snapshot(self) -> None:
        with mock.patch.object(bd, "fetch_text", side_effect=OSError("down")):
            nets = bd.load_cloudflare_ipv4_networks()
        self.assertTrue(bd.is_cloudflare_official_ip("104.16.1.1", nets))

    def test_later_source_fills_missing_country_hint(self) -> None:
        by_ip: dict = {}
        bd.add_candidate(by_ip, {"ip": "10.0.0.5", "country_hint": None, "sources": ["a"]}, [])
        bd.add_candidate(by_ip, {"ip": "10.0.0.5", "country_hint": "US", "sources": ["b"]}, [])
        self.assertEqual(by_ip["10.0.0.5"]["country_hint"], "US")
        self.assertEqual(by_ip["10.0.0.5"]["sources"], ["a", "b"])


class LocalThroughputAgeTest(unittest.TestCase):
    def test_old_local_throughput_is_ignored(self) -> None:
        x = {"ip": "10.0.0.6"}
        local = {"_age_hours": bd.THROUGHPUT_MAX_AGE_HOURS + 1, "throughput": {"10.0.0.6": {"ok": True, "mbps": 9.0}}}
        bd.refresh_throughput(x, {}, local)
        self.assertIsNone(x["eff_throughput_mbps"])

    def test_fresh_local_throughput_is_used(self) -> None:
        x = {"ip": "10.0.0.6"}
        local = {"_age_hours": 1, "throughput": {"10.0.0.6": {"ok": True, "mbps": 9.0}}}
        bd.refresh_throughput(x, {}, local)
        self.assertEqual(x["eff_throughput_mbps"], 9.0)


class KvNamespaceTest(unittest.TestCase):
    def test_reads_binding_not_first_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrangler.toml"
            path.write_text('name = "x"\naccount_id = "acc123"\n\n[[kv_namespaces]]\nbinding = "PROXYIP_KV"\nid = "ns456"\n', encoding="utf-8")
            self.assertEqual(_common.kv_namespace_id(path), "ns456")


class WorkerFixesTest(unittest.TestCase):
    def test_best_txt_n_bypasses_kv_text(self) -> None:
        self.assertIn('url.pathname === "/best.txt" && url.searchParams.has("n")', WORKER)

    def test_kv_text_honours_if_none_match_with_same_max_age(self) -> None:
        self.assertIn('request.headers.get("if-none-match") === textEtag', WORKER)
        self.assertIn("{ etag: textEtag } }), true, false, cacheSeconds)", WORKER)

    def test_hmac_cache_is_bounded(self) -> None:
        self.assertIn("HMAC_CACHE_MAX", WORKER)
        self.assertIn("hmacResultCache.clear()", WORKER)

    def test_cookie_page_is_not_publicly_cached(self) -> None:
        self.assertIn('if (cookie) response.headers.set("cache-control", "private, no-store")', WORKER)


class UnpushedCommitsTest(unittest.TestCase):
    def test_rev_list_failure_raises_instead_of_reporting_zero(self) -> None:
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="bad revision")
        with mock.patch.object(auto_update, "run"), mock.patch.object(auto_update.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "bad revision"):
                auto_update.unpushed_commits()

    def test_counts_commits_ahead(self) -> None:
        ok = subprocess.CompletedProcess(["git"], 0, stdout="2\n", stderr="")
        with mock.patch.object(auto_update, "run"), mock.patch.object(auto_update.subprocess, "run", return_value=ok):
            self.assertEqual(auto_update.unpushed_commits(), 2)


if __name__ == "__main__":
    unittest.main()
