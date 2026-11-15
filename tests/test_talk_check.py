"""Unit tests for .devin/skills/omv-nextcloud-ops/scripts/talk-check.py.

All network I/O is stubbed — urllib.request.urlopen, socket.getaddrinfo,
socket.socket and socket.create_connection are mocked. Nothing touches the
live signal.cloudless.gr / cloud.cloudless.gr endpoints.

    python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import json
import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".devin/skills/omv-nextcloud-ops/scripts/talk-check.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("talk_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["talk_check"] = mod
    spec.loader.exec_module(mod)
    return mod


tc = _load_module()


def _binding_response(xip: int, xport: int, tid: bytes = bytes(range(1, 13))) -> bytes:
    """Craft a STUN Binding Response carrying an XOR-MAPPED-ADDRESS."""
    magic = tc.MAGIC
    attr_val = struct.pack("!BBHI", 0, 0x01, xport ^ (magic >> 16), xip ^ magic)
    attr = struct.pack("!HH", 0x0020, len(attr_val)) + attr_val
    attr += b"\x00" * ((4 - len(attr_val) % 4) % 4)
    return struct.pack("!HHI", 0x0101, len(attr), magic) + tid + attr


def _ip_int(ip: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip))[0]


class _FakeHTTPResp:
    def __init__(self, status: int, body: bytes = b"{}"):
        self.status = status
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HttpProbeTests(unittest.TestCase):
    def test_ok_on_200(self):
        with mock.patch.object(tc.urllib.request, "urlopen", return_value=_FakeHTTPResp(200, b"ok")) as m:
            r = tc.http_probe("https://example.test/x")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["body"], "ok")
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), "talk-check/1.0")

    def test_not_ok_on_non_200(self):
        with mock.patch.object(tc.urllib.request, "urlopen", return_value=_FakeHTTPResp(503)):
            r = tc.http_probe("https://example.test/x")
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 503)

    def test_error_on_exception(self):
        with mock.patch.object(tc.urllib.request, "urlopen", side_effect=OSError("conn refused")):
            r = tc.http_probe("https://example.test/x")
        self.assertFalse(r["ok"])
        self.assertIn("conn refused", r["error"])


class _FakeSock:
    def __init__(self, recv_data: bytes = b"", recv_exc: Exception | None = None):
        self._data = recv_data
        self._exc = recv_exc
        self.sent = b""
        self.peer = None

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        self.sent = data
        self.peer = addr

    def sendall(self, data):
        self.sent = data

    def recvfrom(self, n):
        if self._exc:
            raise self._exc
        return self._data, self.peer or ("127.0.0.1", 3478)

    def recv(self, n):
        if self._exc:
            raise self._exc
        return self._data[:n]

    def close(self):
        pass


class StunProbeTests(unittest.TestCase):
    def _addrinfo(self, host, port, *a):
        return [(socket.AF_INET, socket.SOCK_DGRAM, 6, "", ("93.184.216.34", port))]

    def test_udp_binding_response_parses_xor_mapped_address(self):
        pkt = _binding_response(_ip_int("198.51.100.7"), 5349)
        fake = _FakeSock(recv_data=pkt)
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=self._addrinfo), \
             mock.patch.object(tc.socket, "socket", return_value=fake):
            r = tc.stun_probe("stun.example.test", 3478)
        self.assertTrue(r["ok"])
        self.assertEqual(r["transport"], "udp")
        self.assertEqual(r["mapped_ip"], "198.51.100.7")
        self.assertEqual(r["mapped_port"], 5349)
        # Request was a proper Binding Request: type 0x0001 + magic cookie.
        mtype, _, magic, _ = struct.unpack("!HHI12s", fake.sent[:20])
        self.assertEqual(mtype, 0x0001)
        self.assertEqual(magic, tc.MAGIC)

    def test_tcp_fallback_when_udp_fails(self):
        pkt = _binding_response(_ip_int("203.0.113.9"), 1000)
        udp = _FakeSock(recv_exc=OSError("udp blocked"))
        tcp = _FakeSock(recv_data=pkt)
        socks = iter([udp, tcp])
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=self._addrinfo), \
             mock.patch.object(tc.socket, "socket", side_effect=lambda *a, **k: next(socks)), \
             mock.patch.object(tc.socket, "create_connection", return_value=tcp):
            r = tc.stun_probe("stun.example.test", 3478)
        self.assertTrue(r["ok"])
        self.assertEqual(r["transport"], "tcp")
        self.assertEqual(r["mapped_ip"], "203.0.113.9")

    def test_dns_failure(self):
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=OSError("nxdomain")):
            r = tc.stun_probe("nonexistent.invalid", 3478)
        self.assertFalse(r["ok"])
        self.assertIn("dns:", r["error"])

    def test_short_response_skipped(self):
        udp = _FakeSock(recv_data=b"\x01\x01")  # < 20 bytes
        tcp = _FakeSock(recv_exc=OSError("no tcp"))
        socks = iter([udp, tcp])
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=self._addrinfo), \
             mock.patch.object(tc.socket, "socket", side_effect=lambda *a, **k: next(socks)), \
             mock.patch.object(tc.socket, "create_connection", return_value=tcp):
            r = tc.stun_probe("stun.example.test", 3478)
        self.assertFalse(r["ok"])
        self.assertIn("no binding response", r["error"])

    def test_wrong_magic_rejected(self):
        pkt = bytearray(_binding_response(_ip_int("198.51.100.7"), 5349))
        pkt[4:8] = struct.pack("!I", 0xDEADBEEF)  # corrupt magic
        udp = _FakeSock(recv_data=bytes(pkt))
        tcp = _FakeSock(recv_exc=OSError("no tcp"))
        socks = iter([udp, tcp])
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=self._addrinfo), \
             mock.patch.object(tc.socket, "socket", side_effect=lambda *a, **k: next(socks)), \
             mock.patch.object(tc.socket, "create_connection", return_value=tcp):
            r = tc.stun_probe("stun.example.test", 3478)
        self.assertFalse(r["ok"])

    def test_binding_response_without_xor_attr_reports_note(self):
        tid = bytes(range(1, 13))
        pkt = struct.pack("!HHI", 0x0101, 0, tc.MAGIC) + tid  # no attributes
        udp = _FakeSock(recv_data=pkt)
        with mock.patch.object(tc.socket, "getaddrinfo", side_effect=self._addrinfo), \
             mock.patch.object(tc.socket, "socket", return_value=udp):
            r = tc.stun_probe("stun.example.test", 3478)
        self.assertTrue(r["ok"])
        self.assertIn("without XOR-MAPPED-ADDRESS", r["note"])


class MainTests(unittest.TestCase):
    def test_json_output_shape(self):
        probes = {
            "signaling": {"ok": True, "status": 200},
            "nextcloud_status": {"ok": False, "error": "down"},
        }
        stuns = {f"{h}:{p}": {"ok": True, "mapped_ip": "1.2.3.4", "mapped_port": 5,
                             "transport": "udp"} for h, p in tc.STUN_SERVERS}
        with mock.patch.object(tc, "http_probe", side_effect=lambda u: probes[
                "signaling" if "signal" in u else "nextcloud_status"]), \
             mock.patch.object(tc, "stun_probe", return_value={"ok": True}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with mock.patch.object(sys, "argv", ["talk-check.py", "--json"]):
                    tc.main()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["signaling"]["ok"])
        self.assertFalse(out["nextcloud_status"]["ok"])
        self.assertEqual(len(out["stun"]), len(tc.STUN_SERVERS))
        self.assertIn("turn_note", out)

    def test_text_output_marks_failures(self):
        def fake_probe(url):
            ok = "status.php" in url  # signaling fails, nextcloud status ok
            return {"ok": ok, "status": 200 if ok else None,
                    "error": None if ok else "boom"}

        with mock.patch.object(tc, "http_probe", side_effect=fake_probe), \
             mock.patch.object(tc, "stun_probe",
                               return_value={"ok": False, "error": "no binding"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with mock.patch.object(sys, "argv", ["talk-check.py"]):
                    tc.main()
        text = buf.getvalue()
        self.assertIn("signaling  FAIL", text)
        self.assertIn("nextcloud  OK", text)


if __name__ == "__main__":
    unittest.main()
