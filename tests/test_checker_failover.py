import unittest
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

import build_dataset as bd

SNIPPETS = {"success": False, "supports_ipv4": False,
            "probe_results": {"ipv4": {"ok": False, "error": "connect() is not available in Snippets"}}}
GOOD = {"success": True, "supports_ipv4": True, "country": "US"}
DEAD = {"success": False, "supports_ipv4": False,
        "probe_results": {"ipv4": {"ok": False, "error": "connection refused"}}}


class CheckerFailoverTests(unittest.TestCase):
    """检测接口故障转移、重试与预算的回归测试。"""

    def fake(self, by_api):
        """fake."""
        def _check(ip, retries=2, api=None, deadline=None):
            d = dict(by_api[api]); d["ip"] = ip; d["checker"] = api
            return d
        return _check

    def test_snippets_error_is_checker_fault(self):
        """snippets error is checker fault."""
        self.assertTrue(bd.checker_fault(SNIPPETS))
        self.assertFalse(bd.checker_fault(DEAD))
        self.assertFalse(bd.checker_fault(GOOD))

    def test_falls_back_to_next_checker(self):
        """falls back to next checker."""
        apis = ["https://a/check", "https://b/check"]
        with mock.patch.object(bd, "CHECK_APIS", apis), \
             mock.patch.object(bd, "check_cmliu", self.fake({apis[0]: SNIPPETS, apis[1]: GOOD})):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertTrue(r["success"])
        self.assertEqual(r["checker"], apis[1])

    def test_real_failure_does_not_try_next_checker(self):
        """real failure does not try next checker."""
        apis = ["https://a/check", "https://b/check"]
        calls = []
        def _check(ip, retries=2, api=None, deadline=None):
            calls.append(api); return dict(DEAD, ip=ip)
        with mock.patch.object(bd, "CHECK_APIS", apis), mock.patch.object(bd, "check_cmliu", _check):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertEqual(calls, [apis[0]])
        self.assertFalse(r.get("checker_error"))

    def test_all_checkers_broken_marks_checker_error(self):
        """all checkers broken marks checker error."""
        apis = ["https://a/check", "https://b/check"]
        with mock.patch.object(bd, "CHECK_APIS", apis), \
             mock.patch.object(bd, "check_cmliu", self.fake({apis[0]: SNIPPETS, apis[1]: SNIPPETS})):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertTrue(r["checker_error"])

    def test_default_checkers_prefer_working_host_and_drop_090227(self):
        """default checkers prefer working host and drop 090227."""
        self.assertEqual(bd.CHECK_APIS[0], "https://check.proxyip.cmliussss.net/check")
        self.assertFalse(any("090227" in u for u in bd.CHECK_APIS))

    def test_run_checks_collects_every_ip(self):
        """run checks collects every ip."""
        with mock.patch.object(bd, "check_with_fallback", lambda ip: dict(GOOD, ip=ip)):
            out = bd.run_checks(["1.1.1.1", "2.2.2.2", "3.3.3.3"], 2)
        self.assertEqual(sorted(r["ip"] for r in out), ["1.1.1.1", "2.2.2.2", "3.3.3.3"])

    def test_log_checker_errors_groups_samples(self):
        """log checker errors groups samples."""
        rows = [{"ip": "1", "checker_error": True, "checker": "a", "error": "HTTPError: 429"}] * 3
        with mock.patch("builtins.print") as p:
            bd.log_checker_errors(rows, "t")
        self.assertIn("x3", p.call_args_list[0].args[0])

    def test_check_cmliu_includes_http_error_body(self):
        """check cmliu includes http error body."""
        api = "https://example.test/check"
        error = HTTPError(api, 429, "Too Many Requests", None, BytesIO(b"rate limit exceeded"))
        with mock.patch.object(bd, "urlopen", side_effect=error):
            result = bd.check_cmliu("1.2.3.4", retries=1, api=api)
        self.assertTrue(result["checker_error"])
        self.assertIn("rate limit exceeded", result["error"])
        self.assertIn("429", result["error"])

    def test_retry_only_faulted_ips_and_keep_first_pass_success(self):
        """retry only faulted ips and keep first pass success."""
        first = [
            dict(GOOD, ip="1.1.1.1", checker="a"),
            {"ip": "2.2.2.2", "success": False, "checker_error": True},
            {"ip": "3.3.3.3", "success": False, "checker_error": True},
        ]
        seen = []
        def fake_run(ips, workers, label="checked", deadline=None):
            seen.extend(ips)
            return [dict(GOOD, ip="2.2.2.2", checker="b")]  # 3.3.3.3 未在预算内重测
        with mock.patch.object(bd, "run_checks", fake_run), mock.patch.object(bd.time, "sleep"), \
             mock.patch("builtins.print"):
            out = {r["ip"]: r for r in bd.retry_checker_faults(first)}
        self.assertEqual(sorted(seen), ["2.2.2.2", "3.3.3.3"])
        self.assertEqual(out["1.1.1.1"]["checker"], "a")
        self.assertTrue(out["2.2.2.2"]["success"])
        self.assertTrue(out["3.3.3.3"]["checker_error"])

    def test_run_checks_stops_after_deadline(self):
        """run checks stops after deadline."""
        with mock.patch.object(bd, "check_with_fallback", lambda ip: dict(GOOD, ip=ip)), \
             mock.patch("builtins.print"):
            out = bd.run_checks(["1.1.1.1", "2.2.2.2"], 1, deadline=bd.time.monotonic() - 1)
        self.assertEqual(out, [])

    def test_deadline_bounds_in_flight_check(self):
        """deadline bounds in flight check."""
        with mock.patch.object(bd, "urlopen") as u:
            with self.assertRaises(bd.DeadlineExceeded):
                bd.check_with_fallback("1.2.3.4", deadline=bd.time.monotonic() + 0.5)
        u.assert_not_called()

    def test_deadline_hit_mid_retry_keeps_first_pass_result(self):
        """deadline hit mid retry keeps first pass result."""
        first = [{"ip": "2.2.2.2", "success": False, "checker_error": True, "checker": "a"}]
        def raising(ip, deadline=None):
            raise bd.DeadlineExceeded(ip)
        with mock.patch.object(bd, "check_with_fallback", raising), mock.patch.object(bd.time, "sleep"), \
             mock.patch("builtins.print"):
            out = bd.retry_checker_faults(first)
        self.assertEqual(out, first)

    def test_retry_skipped_when_script_ran_too_long(self):
        """retry skipped when script ran too long."""
        first = [{"ip": "2.2.2.2", "success": False, "checker_error": True}]
        with mock.patch.object(bd, "SCRIPT_STARTED", bd.time.monotonic() - 10_000), \
             mock.patch.object(bd, "run_checks") as rc, mock.patch("builtins.print"):
            out = bd.retry_checker_faults(first)
        rc.assert_not_called()
        self.assertEqual(out, first)

    def test_body_read_stops_at_deadline(self):
        """slow drip body read is bounded by deadline."""
        class Drip:
            def read(self, n):
                return b"x"
        with self.assertRaises(bd.DeadlineExceeded):
            bd._read_bounded(Drip(), bd.time.monotonic() + 0.05)

    def test_direct_https_raises_when_deadline_passed(self):
        """direct https check honours an expired deadline."""
        with mock.patch.object(bd.socket, "create_connection") as cc:
            with self.assertRaises(bd.DeadlineExceeded):
                bd.check_https_direct("1.2.3.4", deadline=bd.time.monotonic() - 1)
        cc.assert_not_called()

    def test_retry_budget_shrinks_to_remaining_job_time(self):
        """retry budget never exceeds time left before the cutoff."""
        first = [{"ip": "2.2.2.2", "success": False, "checker_error": True}]
        seen = {}
        def fake_run(ips, workers, label="checked", deadline=None):
            seen["left"] = deadline - bd.time.monotonic()
            return []
        with mock.patch.object(bd, "SCRIPT_STARTED", bd.time.monotonic() - (bd.CHECKER_RETRY_CUTOFF_S - 100)), \
             mock.patch.object(bd, "CHECKER_RETRY_DELAY_S", 0), mock.patch.object(bd, "run_checks", fake_run), \
             mock.patch("builtins.print"):
            bd.retry_checker_faults(first)
        self.assertLessEqual(seen["left"], 100)

    def test_http_error_body_not_read_under_deadline(self):
        """error body is skipped during the deadline-bound retry pass."""
        api = "https://example.test/check"
        body = mock.Mock()
        error = HTTPError(api, 429, "Too Many Requests", None, body)
        with mock.patch.object(bd, "urlopen", side_effect=error):
            result = bd.check_cmliu("1.2.3.4", retries=1, api=api, deadline=bd.time.monotonic() + 30)
        body.read.assert_not_called()
        self.assertIn("429", result["error"])


if __name__ == "__main__":
    unittest.main()
