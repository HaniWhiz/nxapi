"""Unit tests for nxos_collector using recorded 'show interface' JSON.

Run offline with the standard library only:
    python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import ssl
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nxos_collector import collector as c  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def cfg(**kw):
    env = {"NXOS_USERNAME": "nxapi-mon", "NXOS_PASSWORD": "secret",
           "NXOS_ALLOWED_CIDRS": "192.168.10.0/24, 2001:db8::/64"}
    env.update(kw)
    return c.Config.from_env(env)


def samples(body):
    """Parse exposition text -> {(metric, interface-or-None): value}."""
    out = {}
    for line in body.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(\w+)(?:\{interface="([^"]*)"\})? (\S+)$', line)
        assert m, f"bad exposition line: {line!r}"
        out[(m.group(1), m.group(2))] = float(m.group(3))
    return out


def fixture_fetcher(name):
    def fetch(ip, config):
        return load(name)
    return fetch


class SingleRow(unittest.TestCase):
    def setUp(self):
        status, ctype, body = c.probe("192.168.10.1", cfg(),
                                      fetch=fixture_fetcher("show_interface_single.json"))
        self.status, self.ctype, self.body = status, ctype, body
        self.s = samples(body)

    def test_ok(self):
        self.assertEqual(self.status, 200)
        self.assertTrue(self.ctype.startswith("text/plain; version=0.0.4"))
        self.assertEqual(self.s[("nxos_up", None)], 1)
        self.assertIn(("nxos_poll_duration_seconds", None), self.s)

    def test_all_counters_string_values(self):
        e = "Ethernet1/1"
        self.assertEqual(self.s[("nxos_interface_in_bytes_total", e)], 61234567890)
        self.assertEqual(self.s[("nxos_interface_out_bytes_total", e)], 63456789012)
        self.assertEqual(self.s[("nxos_interface_in_unicast_packets_total", e)], 88123412)
        self.assertEqual(self.s[("nxos_interface_crc_errors_total", e)], 3)
        self.assertEqual(self.s[("nxos_interface_out_discards_total", e)], 17)
        self.assertEqual(self.s[("nxos_interface_oper_up", e)], 1)
        self.assertEqual(self.s[("nxos_interface_admin_up", e)], 1)
        counters = [k for k in self.s if k[0].endswith("_total")]
        self.assertEqual(len(counters), 13)

    def test_rate_keys_not_exported(self):
        self.assertNotIn(b"rate1", self.body)

    def test_no_switch_or_dc_labels(self):
        for line in self.body.decode().splitlines():
            if not line.startswith("#"):
                self.assertNotIn("switch=", line)
                self.assertNotIn("dc=", line)

    def test_type_lines(self):
        text = self.body.decode()
        self.assertIn("# TYPE nxos_interface_crc_errors_total counter", text)
        self.assertIn("# TYPE nxos_interface_oper_up gauge", text)
        self.assertIn("# TYPE nxos_up gauge", text)


class MultiRow(unittest.TestCase):
    def run_probe(self, **kw):
        status, _, body = c.probe("192.168.10.1", cfg(**kw),
                                  fetch=fixture_fetcher("show_interface_multi.json"))
        self.assertEqual(status, 200)
        return samples(body)

    def test_only_ethernet(self):
        s = self.run_probe()
        ifaces = {k[1] for k in s if k[1]}
        self.assertEqual(ifaces, {"Ethernet1/1", "Ethernet1/2", "Ethernet1/48"})

    def test_numeric_values(self):
        s = self.run_probe()
        self.assertEqual(s[("nxos_interface_in_bytes_total", "Ethernet1/1")], 61234567890)

    def test_missing_key_skipped(self):
        s = self.run_probe()
        self.assertNotIn(("nxos_interface_crc_errors_total", "Ethernet1/2"), s)
        self.assertEqual(s[("nxos_interface_in_errors_total", "Ethernet1/2")], 42)
        self.assertNotIn(("nxos_interface_in_errors_total", "Ethernet1/48"), s)

    def test_states(self):
        s = self.run_probe()
        self.assertEqual(s[("nxos_interface_oper_up", "Ethernet1/2")], 0)
        self.assertEqual(s[("nxos_interface_admin_up", "Ethernet1/2")], 1)
        self.assertEqual(s[("nxos_interface_admin_up", "Ethernet1/48")], 0)

    def test_exclude_regex(self):
        s = self.run_probe(NXOS_EXCLUDE_REGEX=r"^Ethernet1/(4[0-9])$")
        self.assertEqual({k[1] for k in s if k[1]}, {"Ethernet1/1", "Ethernet1/2"})

    def test_families_are_contiguous(self):
        _, _, body = c.probe("192.168.10.1", cfg(),
                             fetch=fixture_fetcher("show_interface_multi.json"))
        seen, last = set(), None
        for line in body.decode().splitlines():
            if line.startswith("#"):
                continue
            name = line.split("{")[0].split(" ")[0]
            if name != last:
                self.assertNotIn(name, seen, f"{name} split across the output")
                seen.add(name)
                last = name


class Failures(unittest.TestCase):
    def test_rbac_error_gives_up_0(self):
        status, _, body = c.probe("192.168.10.1", cfg(),
                                  fetch=fixture_fetcher("show_interface_rbac_error.json"))
        self.assertEqual(status, 200)
        s = samples(body)
        self.assertEqual(s[("nxos_up", None)], 0)
        self.assertEqual([k for k in s if k[1]], [])

    def test_fetch_error_gives_up_0(self):
        def boom(ip, config):
            raise c.ProbeError("timed out")
        _, _, body = c.probe("192.168.10.1", cfg(), fetch=boom)
        self.assertEqual(samples(body)[("nxos_up", None)], 0)

    def test_garbage_reply_gives_up_0(self):
        for reply in (None, [], "x", [1], {"result": None}, {"result": {"body": {}}}):
            _, _, body = c.probe("192.168.10.1", cfg(), fetch=lambda ip, conf, r=reply: r)
            self.assertEqual(samples(body)[("nxos_up", None)], 0, reply)

    def test_missing_credentials(self):
        conf = c.Config.from_env({"NXOS_ALLOWED_CIDRS": "192.168.10.0/24"})
        _, _, body = c.probe("192.168.10.1", conf)  # real fetcher, fails before network
        self.assertEqual(samples(body)[("nxos_up", None)], 0)


class TargetValidation(unittest.TestCase):
    def never(self, ip, config):
        raise AssertionError("must not contact the switch")

    def test_outside_allow_list(self):
        self.assertEqual(c.probe("10.0.0.1", cfg(), fetch=self.never)[0], 403)

    def test_hostname_refused(self):
        self.assertEqual(c.probe("evil.example.com", cfg(), fetch=self.never)[0], 400)
        self.assertEqual(c.probe("192.168.10.1:22", cfg(), fetch=self.never)[0], 400)
        self.assertEqual(c.probe("192.168.10.1/ins", cfg(), fetch=self.never)[0], 400)

    def test_missing_target(self):
        self.assertEqual(c.probe("", cfg(), fetch=self.never)[0], 400)

    def test_empty_allow_list_refuses_all(self):
        conf = c.Config.from_env({"NXOS_USERNAME": "u", "NXOS_PASSWORD": "p"})
        self.assertEqual(c.probe("192.168.10.1", conf, fetch=self.never)[0], 403)

    def test_ipv6(self):
        ok = fixture_fetcher("show_interface_single.json")
        self.assertEqual(c.probe("2001:db8::1", cfg(), fetch=ok)[0], 200)
        self.assertEqual(c.probe("[2001:db8::1]", cfg(), fetch=ok)[0], 200)
        self.assertEqual(c.probe("2001:db9::1", cfg(), fetch=self.never)[0], 403)

    def test_disabled(self):
        conf = cfg(NXOS_PROBE_ENABLED="false")
        self.assertEqual(c.probe("192.168.10.1", conf, fetch=self.never)[0], 404)

    def test_query_string(self):
        c._config = cfg()
        try:
            self.assertEqual(c.probe_from_query("target=10.1.1.1")[0], 403)
            self.assertEqual(c.probe_from_query("")[0], 400)
        finally:
            c._config = None


class Helpers(unittest.TestCase):
    def test_as_list(self):
        self.assertEqual(c.as_list(None), [])
        self.assertEqual(c.as_list({"a": 1}), [{"a": 1}])
        self.assertEqual(c.as_list([1, 2]), [1, 2])

    def test_to_number(self):
        self.assertEqual(c.to_number("42"), 42)
        self.assertEqual(c.to_number(" 42 "), 42)
        self.assertEqual(c.to_number(42), 42)
        self.assertEqual(c.to_number("18446744073709551615"), 18446744073709551615)
        self.assertEqual(c.to_number(1.0), 1)
        self.assertIsNone(c.to_number("n/a"))
        self.assertIsNone(c.to_number(True))
        self.assertIsNone(c.to_number(None))

    def test_label_escape(self):
        self.assertEqual(c._escape('a"b\\c\nd'), 'a\\"b\\\\c\\nd')

    def test_password_not_in_repr(self):
        self.assertNotIn("secret", repr(cfg()))

    def test_password_file(self):
        path = os.path.join(FIXTURES, "..", "_pw.tmp")
        with open(path, "w") as f:
            f.write("from-file\n")
        try:
            self.assertEqual(cfg(NXOS_PASSWORD_FILE=path).get_password(), "from-file")
        finally:
            os.remove(path)

    def test_tls(self):
        ctx = c._ssl_context(cfg())
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_defaults(self):
        conf = cfg()
        self.assertEqual(conf.timeout, 15.0)
        self.assertEqual(conf.port, 443)
        self.assertTrue(conf.enabled)


if __name__ == "__main__":
    unittest.main()
