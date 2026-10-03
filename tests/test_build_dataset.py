from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dataset", ROOT / "build_dataset.py")
build_dataset = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(build_dataset)


def make_item(
    ip: str,
    bot_score: int = 95,
    latency: int = 100,
    jitter: int = 0,
    country: str = "US",
    colo: str = "IAD",
    stability: dict | None = None,
) -> dict:
    item = {
        "ip": ip,
        "success": True,
        "supports_ipv4": True,
        "latency_ms": latency,
        "rtt_p50_ms": latency,
        "rtt_jitter_ms": jitter,
        "rtt_samples": 3,
        "rtt_ok": True,
        "probe_attempted": True,
        "probe_results": {"ipv4": {"exit": {
            "country": country,
            "colo": colo,
            "asn": 1,
            "botManagement": {"score": bot_score, "corporateProxy": False, "verifiedBot": False},
        }}},
    }
    return build_dataset.enrich(item, {"sources": ["test"]}, stability)


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


class ProbeTest(unittest.TestCase):
    def test_probe_rtt_reports_p50_and_jitter(self) -> None:
        values = iter([100, 120, 300])
        original = build_dataset.tcp_tls_rtt
        build_dataset.tcp_tls_rtt = lambda ip, timeout=5: next(values)
        try:
            probe = build_dataset.probe_rtt("1.2.3.4", samples=3, timeout=5)
        finally:
            build_dataset.tcp_tls_rtt = original

        self.assertEqual(probe["rtt_p50_ms"], 120)
        self.assertEqual(probe["rtt_jitter_ms"], 200)
        self.assertEqual(probe["rtt_samples"], 3)
        self.assertTrue(probe["rtt_ok"])

    def test_probe_rtt_aborts_after_repeated_failures(self) -> None:
        def boom(*args, **kwargs):
            raise OSError("refused")

        original = build_dataset.tcp_tls_rtt
        build_dataset.tcp_tls_rtt = boom
        try:
            probe = build_dataset.probe_rtt("1.2.3.4", samples=3, timeout=1)
        finally:
            build_dataset.tcp_tls_rtt = original

        self.assertFalse(probe["rtt_ok"])
        self.assertIsNone(probe["rtt_p50_ms"])
        self.assertEqual(probe["rtt_samples"], 0)

    def test_apply_probe_replaces_third_party_latency(self) -> None:
        item = {"ip": "1.2.3.4", "latency_ms": 640, "probe_results": {"ipv4": {"connect_ms": 5, "tls_ms": 9}}}
        build_dataset.apply_probe(item, {"rtt_p50_ms": 130, "rtt_jitter_ms": 40, "rtt_samples": 3, "rtt_ok": True})

        self.assertEqual(item["latency_ms"], 130)
        self.assertEqual(item["api_latency_ms"], 640)
        self.assertEqual(item["api_connect_ms"], 5)
        self.assertEqual(item["api_tls_ms"], 9)
        self.assertTrue(item["probe_attempted"])


class GateThenSpeedTest(unittest.TestCase):
    def test_faster_ip_wins_even_with_lower_bot_score(self) -> None:
        fast = make_item("10.0.0.1", bot_score=85, latency=100)
        slow = make_item("10.0.0.2", bot_score=100, latency=2500)

        self.assertTrue(build_dataset.passes_quality_gate(fast))
        self.assertTrue(build_dataset.passes_quality_gate(slow))
        self.assertLess(build_dataset.rank_key(fast), build_dataset.rank_key(slow))
        self.assertGreater(build_dataset.stable_score(fast), build_dataset.stable_score(slow))

    def test_low_bot_score_is_gated_out(self) -> None:
        good = make_item("10.0.0.1", bot_score=90, latency=400)
        bad = make_item("10.0.0.2", bot_score=50, latency=90)

        self.assertTrue(build_dataset.passes_quality_gate(good))
        self.assertFalse(build_dataset.passes_quality_gate(bad))
        self.assertLess(build_dataset.rank_key(good), build_dataset.rank_key(bad))

    def test_high_jitter_is_gated_out(self) -> None:
        steady = make_item("10.0.0.1", latency=150, jitter=20)
        jumpy = make_item("10.0.0.2", latency=150, jitter=build_dataset.MAX_JITTER_MS + 1)

        self.assertTrue(build_dataset.passes_quality_gate(steady))
        self.assertFalse(build_dataset.passes_quality_gate(jumpy))
        self.assertLess(build_dataset.rank_key(steady), build_dataset.rank_key(jumpy))

    def test_unreachable_probe_fails_latency_gate(self) -> None:
        item = make_item("10.0.0.1", latency=100)
        build_dataset.apply_probe(item, {"rtt_p50_ms": None, "rtt_jitter_ms": None, "rtt_samples": 0, "rtt_ok": False})
        build_dataset.score_item(item)

        self.assertEqual(item["latency_ms"], build_dataset.PROBE_TIMEOUT * 1000)
        self.assertFalse(build_dataset.passes_quality_gate(item))

    def test_partial_probe_samples_are_gated_out(self) -> None:
        measured = make_item("10.0.0.1", latency=1100)
        partial = make_item("10.0.0.2", latency=1090)
        partial["rtt_samples"] = 1
        build_dataset.score_item(partial)

        self.assertTrue(build_dataset.passes_quality_gate(measured))
        self.assertFalse(build_dataset.passes_quality_gate(partial))
        self.assertGreater(build_dataset.rank_penalty(partial), build_dataset.rank_penalty(measured))
        self.assertLess(build_dataset.rank_key(measured), build_dataset.rank_key(partial))

    def test_unprobed_item_is_not_penalised_for_sample_deficit(self) -> None:
        item = make_item("10.0.0.1", latency=100)
        item["rtt_samples"] = 1
        item.pop("probe_attempted", None)

        self.assertEqual(build_dataset.probe_sample_deficit(item), 0)
        self.assertTrue(build_dataset.passes_quality_gate(item))


class StabilityHistoryTest(unittest.TestCase):
    def test_stability_for_uses_rolling_window(self) -> None:
        history = {
            "10.0.0.1": {"recent": "1111111111", "lats": "100,110", "last_checked_at": "2026-10-03T00:00:00+00:00"},
            "10.0.0.2": {"recent": "1100110011", "lats": "100,110", "last_checked_at": "2026-10-03T00:00:00+00:00"},
        }

        stable = build_dataset.stability_for(history, "10.0.0.1")
        unstable = build_dataset.stability_for(history, "10.0.0.2")

        self.assertEqual(stable["check_count"], 10)
        self.assertEqual(stable["success_rate_7d"], 1.0)
        self.assertEqual(stable["effective_success_rate"], 1.0)
        self.assertTrue(stable["gate_ok"])
        self.assertFalse(stable["cold_start"])

        self.assertEqual(unstable["success_rate_7d"], 0.6)
        self.assertLess(unstable["effective_success_rate"], 1.0)
        self.assertFalse(unstable["gate_ok"])

    def test_unstable_candidate_ranks_below_stable_at_same_speed(self) -> None:
        history = {
            "10.0.0.1": {"recent": "1111111111", "lats": "100", "last_checked_at": "2026-10-03T00:00:00+00:00"},
            "10.0.0.2": {"recent": "1100110011", "lats": "100", "last_checked_at": "2026-10-03T00:00:00+00:00"},
        }
        stable = make_item("10.0.0.1", latency=100, stability=build_dataset.stability_for(history, "10.0.0.1"))
        unstable = make_item("10.0.0.2", latency=100, stability=build_dataset.stability_for(history, "10.0.0.2"))

        self.assertLess(build_dataset.rank_key(stable), build_dataset.rank_key(unstable))
        self.assertGreater(build_dataset.stable_score(stable), build_dataset.stable_score(unstable))

    def test_cold_start_never_evicts_current_ip(self) -> None:
        cold = build_dataset.stability_for({}, "10.0.0.9")
        sparse = build_dataset.stability_for(
            {"10.0.0.9": {"recent": "0", "lats": "", "last_checked_at": "2026-10-03T00:00:00+00:00"}},
            "10.0.0.9",
        )

        self.assertTrue(cold["gate_ok"])
        self.assertTrue(sparse["gate_ok"])
        self.assertEqual(sparse["check_count"], 1)
        self.assertTrue(build_dataset.current_quality_ok(make_item("10.0.0.9", stability=sparse)))

    def test_established_unstable_current_ip_fails_quality(self) -> None:
        weak = build_dataset.stability_for(
            {"10.0.0.9": {"recent": "111110", "lats": "100", "last_checked_at": "2026-10-03T00:00:00+00:00"}},
            "10.0.0.9",
        )
        item = make_item("10.0.0.9", latency=100, stability=weak)

        self.assertEqual(weak["check_count"], build_dataset.MIN_HISTORY_CHECKS)
        self.assertFalse(weak["gate_ok"])
        self.assertFalse(build_dataset.current_quality_ok(item))


class ThroughputTest(unittest.TestCase):
    def test_unmeasured_throughput_is_the_most_expensive_term(self) -> None:
        self.assertEqual(build_dataset.transfer_ms(None), build_dataset.THROUGHPUT_UNKNOWN_MS)
        self.assertLess(build_dataset.transfer_ms(20), build_dataset.transfer_ms(1))
        self.assertLess(build_dataset.transfer_ms(1), build_dataset.transfer_ms(None))

    def test_measured_fast_pipe_beats_unmeasured_fast_handshake(self) -> None:
        measured = make_item("10.0.0.1", latency=1150)
        measured["eff_throughput_mbps"] = 25.0
        build_dataset.score_item(measured)
        unmeasured = make_item("10.0.0.2", latency=1000)
        build_dataset.score_item(unmeasured)

        self.assertTrue(build_dataset.passes_quality_gate(measured))
        self.assertTrue(build_dataset.passes_quality_gate(unmeasured))
        self.assertLess(build_dataset.rank_key(measured), build_dataset.rank_key(unmeasured))

    def test_fast_handshake_with_slow_pipe_loses(self) -> None:
        fast_handshake = make_item("10.0.0.1", latency=900)
        fast_handshake["eff_throughput_mbps"] = 0.5
        build_dataset.score_item(fast_handshake)
        slow_handshake = make_item("10.0.0.2", latency=1500)
        slow_handshake["eff_throughput_mbps"] = 25.0
        build_dataset.score_item(slow_handshake)

        self.assertLess(build_dataset.rank_key(slow_handshake), build_dataset.rank_key(fast_handshake))

    def test_current_ip_quality_fails_below_min_throughput(self) -> None:
        item = make_item("10.0.0.1", latency=100)
        self.assertTrue(build_dataset.current_quality_ok(item))

        item["eff_throughput_mbps"] = build_dataset.THROUGHPUT_MIN_MBPS / 2
        build_dataset.score_item(item)
        self.assertFalse(build_dataset.current_quality_ok(item))

        item["eff_throughput_mbps"] = build_dataset.THROUGHPUT_MIN_MBPS * 5
        build_dataset.score_item(item)
        self.assertTrue(build_dataset.current_quality_ok(item))

    def test_combine_throughput_prefers_local_measurements(self) -> None:
        combined = build_dataset.combine_throughput(4.0, 8.0, None)
        expected = (4.0 * build_dataset.THROUGHPUT_CI_WEIGHT + 8.0 * build_dataset.LOCAL_THROUGHPUT_WEIGHT) / (
            build_dataset.THROUGHPUT_CI_WEIGHT + build_dataset.LOCAL_THROUGHPUT_WEIGHT
        )
        self.assertAlmostEqual(combined, round(expected, 3))
        self.assertEqual(build_dataset.combine_throughput(None, 8.0, None), 8.0)
        self.assertEqual(build_dataset.combine_throughput(None, None, 3.5), 3.5)
        self.assertIsNone(build_dataset.combine_throughput(None, None, None))

    def test_refresh_throughput_uses_fresh_history_and_expires_stale(self) -> None:
        fresh = make_item("10.0.0.1", latency=100)
        build_dataset.refresh_throughput(
            fresh, {"10.0.0.1": {"mbps": 9.5, "mbps_at": datetime.now(timezone.utc).isoformat()}}, None
        )
        self.assertEqual(fresh["eff_throughput_mbps"], 9.5)
        self.assertEqual(fresh["throughput_source"], "history")

        stale = make_item("10.0.0.2", latency=100)
        build_dataset.refresh_throughput(
            stale, {"10.0.0.2": {"mbps": 9.5, "mbps_at": "2020-01-01T00:00:00+00:00"}}, None
        )
        self.assertIsNone(stale["eff_throughput_mbps"])

    def test_throughput_pool_always_includes_current_ip(self) -> None:
        original = build_dataset.THROUGHPUT_TOP_N
        build_dataset.THROUGHPUT_TOP_N = 2
        try:
            items = [make_item("10.0.0.1"), make_item("10.0.0.2"), make_item("10.0.0.3")]
            pool = build_dataset.throughput_pool(items, "10.0.0.3")
        finally:
            build_dataset.THROUGHPUT_TOP_N = original

        self.assertEqual([x["ip"] for x in pool], ["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_apply_throughput_records_success_and_failure(self) -> None:
        item: dict = {}
        build_dataset.apply_throughput(
            item, {"ok": True, "mbps": 4.2, "bytes": 1048576, "ttfb_ms": 30, "duration_ms": 249, "complete": True}
        )
        self.assertEqual(item["throughput_mbps"], 4.2)
        self.assertTrue(item["throughput_complete"])
        self.assertTrue(item["throughput_attempted"])

        failed: dict = {}
        build_dataset.apply_throughput(failed, {"ok": False, "mbps": None, "error": "timeout"})
        self.assertIsNone(failed["throughput_mbps"])
        self.assertTrue(failed["throughput_attempted"])


class LocalProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original = build_dataset.LOCAL_PROBE_PATH
        build_dataset.LOCAL_PROBE_PATH = Path(self._tmp.name) / "probe_local.json"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        build_dataset.LOCAL_PROBE_PATH = self._original
        self._tmp.cleanup()

    def _write(self, payload: dict) -> None:
        build_dataset.LOCAL_PROBE_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def test_fresh_file_is_loaded_with_age(self) -> None:
        now = datetime.now(timezone.utc)
        self._write({"probed_at": now.isoformat(), "rtt": {"10.0.0.1": {"rtt_ok": True}}, "throughput": {}})

        loaded = build_dataset.load_local_probe(now)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["_age_hours"], 0)

    def test_stale_file_is_ignored(self) -> None:
        now = datetime.now(timezone.utc)
        stale = now.timestamp() - (build_dataset.LOCAL_PROBE_MAX_AGE_HOURS + 1) * 3600
        self._write({
            "probed_at": datetime.fromtimestamp(stale, timezone.utc).isoformat(),
            "rtt": {"10.0.0.1": {"rtt_ok": True}},
            "throughput": {},
        })

        self.assertIsNone(build_dataset.load_local_probe(now))

    def test_merge_local_probe_blends_rtt_and_keeps_worse_jitter(self) -> None:
        item = make_item("10.0.0.1", latency=1000, jitter=50)
        local = {
            "rtt": {"10.0.0.1": {"rtt_p50_ms": 600, "rtt_jitter_ms": 400, "rtt_samples": 3, "rtt_ok": True}},
            "throughput": {"10.0.0.1": {"ok": True, "mbps": 6.0}},
        }

        merged = build_dataset.merge_local_probe([item], local)

        expected = round(
            build_dataset.LOCAL_PROBE_WEIGHT * 600 + (1 - build_dataset.LOCAL_PROBE_WEIGHT) * 1000
        )
        self.assertEqual(merged, 1)
        self.assertEqual(item["rtt_p50_ms"], expected)
        self.assertEqual(item["latency_ms"], expected)
        self.assertEqual(item["rtt_jitter_ms"], 400)
        self.assertEqual(item["rtt_samples_effective"], 3)
        self.assertEqual(item["local_throughput_mbps"], 6.0)

    def test_merge_local_probe_rescues_failed_ci_probe(self) -> None:
        item = make_item("10.0.0.1", latency=1000)
        build_dataset.apply_probe(
            item, {"rtt_p50_ms": None, "rtt_jitter_ms": None, "rtt_samples": 0, "rtt_ok": False}
        )
        local = {"rtt": {"10.0.0.1": {"rtt_p50_ms": 700, "rtt_jitter_ms": 60, "rtt_samples": 3, "rtt_ok": True}}}

        build_dataset.merge_local_probe([item], local)

        self.assertTrue(item["rtt_ok"])
        self.assertEqual(item["rtt_p50_ms"], 700)
        self.assertEqual(item["latency_ms"], 700)
        self.assertEqual(build_dataset.probe_sample_deficit(item), 0)


class IpHistoryStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original = build_dataset.IP_HISTORY_PATH
        build_dataset.IP_HISTORY_PATH = Path(self._tmp.name) / "ip_history.json"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        build_dataset.IP_HISTORY_PATH = self._original
        self._tmp.cleanup()

    def _write(self, payload: dict) -> None:
        build_dataset.IP_HISTORY_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def test_success_comes_from_probe_not_from_pool_membership(self) -> None:
        results = [
            {"ip": "1.1.1.1", "success": True, "supports_ipv4": True, "probe_attempted": True, "rtt_ok": True, "rtt_p50_ms": 120},
            {"ip": "2.2.2.2", "success": False, "supports_ipv4": False},
            {"ip": "3.3.3.3", "success": True, "supports_ipv4": True, "probe_attempted": True, "rtt_ok": False},
        ]

        history = build_dataset.update_ip_history(results, "2026-10-03T00:00:00+00:00")

        self.assertEqual(history["1.1.1.1"]["recent"], "1")
        self.assertEqual(history["1.1.1.1"]["lats"], "120")
        self.assertEqual(history["2.2.2.2"]["recent"], "0")
        self.assertEqual(history["3.3.3.3"]["recent"], "0")
        self.assertEqual(history["3.3.3.3"]["lats"], "")
        self.assertEqual(history["1.1.1.1"]["last_checked_at"], "2026-10-03T00:00:00+00:00")

    def test_window_is_bounded(self) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        results = [{"ip": "1.1.1.1", "success": True, "supports_ipv4": True, "probe_attempted": True, "rtt_ok": True, "rtt_p50_ms": 100}]
        history: dict = {}
        for _ in range(build_dataset.HISTORY_WINDOW + 10):
            build_dataset.IP_HISTORY_PATH.write_text(json.dumps(history), encoding="utf-8")
            history = build_dataset.update_ip_history(results, checked_at)

        self.assertLessEqual(len(history["1.1.1.1"]["recent"]), build_dataset.HISTORY_WINDOW)
        self.assertLessEqual(len(history["1.1.1.1"]["lats"].split(",")), build_dataset.HISTORY_LAT_WINDOW)

    def test_stale_records_are_pruned_on_load(self) -> None:
        self._write({
            "1.1.1.1": {"recent": "1", "lats": "100", "last_checked_at": "2026-01-01T00:00:00+00:00"},
            "2.2.2.2": {"recent": "1", "lats": "100", "last_checked_at": datetime.now(timezone.utc).isoformat()},
            "3.3.3.3": {"checks": [{"checked_at": "2026-01-01T00:00:00+00:00", "success": True, "latency_ms": 50}]},
        })

        loaded = build_dataset.load_ip_history()

        self.assertNotIn("1.1.1.1", loaded)
        self.assertIn("2.2.2.2", loaded)
        self.assertNotIn("3.3.3.3", loaded)

    def test_legacy_checks_record_is_migrated(self) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        self._write({
            "5.5.5.5": {
                "checks": [
                    {"checked_at": checked_at, "success": True, "latency_ms": 80},
                    {"checked_at": checked_at, "success": False, "latency_ms": None},
                ],
                "last_checked_at": checked_at,
            },
        })

        loaded = build_dataset.load_ip_history()
        record = loaded["5.5.5.5"]

        self.assertNotIn("checks", record)
        self.assertEqual(record["recent"], "10")
        self.assertEqual(record["lats"], "80")
        self.assertEqual(record["success_rate_7d"], 0.5)
        self.assertEqual(record["avg_latency_ms_recent"], 80.0)

    def test_throughput_is_recorded_carried_forward_and_expired(self) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        results = [
            {"ip": "1.1.1.1", "success": True, "supports_ipv4": True, "probe_attempted": True, "rtt_ok": True, "rtt_p50_ms": 100}
        ]

        recorded = build_dataset.update_ip_history(
            results, checked_at, {"1.1.1.1": {"ok": True, "mbps": 7.5, "bytes": 10485760}}
        )
        self.assertEqual(recorded["1.1.1.1"]["mbps"], 7.5)
        self.assertEqual(recorded["1.1.1.1"]["mbps_at"], checked_at)

        self._write(recorded)
        carried = build_dataset.update_ip_history(results, checked_at, {"1.1.1.1": {"ok": False, "mbps": None}})
        self.assertEqual(carried["1.1.1.1"]["mbps"], 7.5)

        self._write(recorded)
        reloaded = build_dataset.load_ip_history()
        self.assertEqual(reloaded["1.1.1.1"]["mbps"], 7.5)

        recorded["1.1.1.1"]["mbps_at"] = "2020-01-01T00:00:00+00:00"
        self._write(recorded)
        expired = build_dataset.update_ip_history(results, checked_at, {})
        self.assertIsNone(expired["1.1.1.1"]["mbps"])
        self.assertIsNone(expired["1.1.1.1"]["mbps_at"])


if __name__ == "__main__":
    unittest.main()
