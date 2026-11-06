import unittest
from unittest import mock

import build_dataset as bd

SNIPPETS = {"success": False, "supports_ipv4": False,
            "probe_results": {"ipv4": {"ok": False, "error": "connect() is not available in Snippets"}}}
GOOD = {"success": True, "supports_ipv4": True, "country": "US"}
DEAD = {"success": False, "supports_ipv4": False,
        "probe_results": {"ipv4": {"ok": False, "error": "connection refused"}}}


class CheckerFailoverTests(unittest.TestCase):
    def fake(self, by_api):
        def _check(ip, retries=2, api=None):
            d = dict(by_api[api]); d["ip"] = ip; d["checker"] = api
            return d
        return _check

    def test_snippets_error_is_checker_fault(self):
        self.assertTrue(bd.checker_fault(SNIPPETS))
        self.assertFalse(bd.checker_fault(DEAD))
        self.assertFalse(bd.checker_fault(GOOD))

    def test_falls_back_to_next_checker(self):
        apis = ["https://a/check", "https://b/check"]
        with mock.patch.object(bd, "CHECK_APIS", apis), \
             mock.patch.object(bd, "check_cmliu", self.fake({apis[0]: SNIPPETS, apis[1]: GOOD})):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertTrue(r["success"])
        self.assertEqual(r["checker"], apis[1])

    def test_real_failure_does_not_try_next_checker(self):
        apis = ["https://a/check", "https://b/check"]
        calls = []
        def _check(ip, retries=2, api=None):
            calls.append(api); return dict(DEAD, ip=ip)
        with mock.patch.object(bd, "CHECK_APIS", apis), mock.patch.object(bd, "check_cmliu", _check):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertEqual(calls, [apis[0]])
        self.assertFalse(r.get("checker_error"))

    def test_all_checkers_broken_marks_checker_error(self):
        apis = ["https://a/check", "https://b/check"]
        with mock.patch.object(bd, "CHECK_APIS", apis), \
             mock.patch.object(bd, "check_cmliu", self.fake({apis[0]: SNIPPETS, apis[1]: SNIPPETS})):
            r = bd.check_with_fallback("1.2.3.4")
        self.assertTrue(r["checker_error"])


if __name__ == "__main__":
    unittest.main()
