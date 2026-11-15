"""Unit tests for .devin/skills/omv-nextcloud-ops/nextcloud-integration/scripts/nextcloud-dav.py.

All HTTP is stubbed at the module's _req seam (and urllib for share parsing);
no live Nextcloud calls are made.

    python3 -m unittest discover -s tests
"""

import argparse
import importlib.util
import io
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".devin/skills/omv-nextcloud-ops/nextcloud-integration/scripts/nextcloud-dav.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nextcloud_dav", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nextcloud_dav"] = mod
    spec.loader.exec_module(mod)
    return mod


dav = _load_module()

DAV_URL = "https://cloud.cloudless.gr/remote.php/dav/files/user@cloudless.gr"


def _args(**kw):
    base = dict(dav_url=DAV_URL, user="user@cloudless.gr",
                password="app-pw", verbose=False,
                label=None, expire=None)
    base.update(kw)
    return argparse.Namespace(**base)


PROPFIND_XML = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/user@cloudless.gr/Docs/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/></d:resourcetype>
    </d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/user@cloudless.gr/Docs/report%20final.pdf</d:href>
    <d:propstat><d:prop>
      <d:getcontenttype>application/pdf</d:getcontenttype>
      <d:getcontentlength>12345</d:getcontentlength>
      <d:resourcetype/>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""


class QuoteTests(unittest.TestCase):
    def test_q_escapes_each_segment(self):
        self.assertEqual(dav._q("a/b c/d+e"), "a/b%20c/d%2Be")
        self.assertEqual(dav._q("/lead/trail/"), "lead/trail")
        self.assertEqual(dav._q(""), "")


class CredsTests(unittest.TestCase):
    def test_flags_supply_all_creds(self):
        url, user, pw = dav._creds(_args())
        self.assertEqual((url, user, pw),
                         (DAV_URL, "user@cloudless.gr", "app-pw"))

    def test_missing_creds_exits(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(dav, "_load_env_file", return_value={}):
            with self.assertRaises(SystemExit):
                dav._creds(argparse.Namespace(dav_url=None, user=None,
                                              password=None))

    def test_env_fallback(self):
        env = {"NEXTCLOUD_DAV_URL": DAV_URL + "/",
               "NEXTCLOUD_USERNAME": "env@x.gr",
               "NEXTCLOUD_APP_PASSWORD": "envpw"}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(dav, "_load_env_file", return_value={}):
            url, user, pw = dav._creds(argparse.Namespace(
                dav_url=None, user=None, password=None))
        self.assertEqual(url, DAV_URL)  # trailing slash stripped
        self.assertEqual((user, pw), ("env@x.gr", "envpw"))


class ReqTests(unittest.TestCase):
    def test_sends_basic_auth_and_returns_status_body(self):
        captured = {}

        class _Resp:
            status = 207

            def read(self):
                return b"payload"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            return _Resp()

        with mock.patch.object(dav.urllib.request, "urlopen", side_effect=fake_urlopen):
            code, body = dav._req("https://x.test/dav", "PROPFIND",
                                  "u@x.gr", "pw", data=b"d",
                                  headers={"Depth": "1"})
        self.assertEqual((code, body), (207, b"payload"))
        req = captured["req"]
        self.assertEqual(req.get_method(), "PROPFIND")
        self.assertEqual(req.get_header("Depth"), "1")
        import base64
        expected = base64.b64encode(b"u@x.gr:pw").decode()
        self.assertEqual(req.get_header("Authorization"), f"Basic {expected}")

    def test_http_error_returns_code_and_body(self):
        import urllib.error

        err = urllib.error.HTTPError("https://x", 404, "nf", {}, io.BytesIO(b"nope"))
        with mock.patch.object(dav.urllib.request, "urlopen", side_effect=err):
            code, body = dav._req("https://x", "GET", "u", "p")
        self.assertEqual(code, 404)
        self.assertEqual(body, b"nope")


class MkdirsTests(unittest.TestCase):
    def test_mkdirs_tolerates_201_and_405(self):
        calls = []

        def fake_req(url, method, user, pw, **kw):
            calls.append((method, url))
            return (201 if len(calls) == 1 else 405), b""

        with mock.patch.object(dav, "_req", side_effect=fake_req):
            dav._mkdirs(DAV_URL, "u", "p", "a/b")
        self.assertEqual([m for m, _ in calls], ["MKCOL", "MKCOL"])
        self.assertTrue(calls[0][1].endswith("/a"))
        self.assertTrue(calls[1][1].endswith("/a/b"))

    def test_mkdirs_exits_on_error(self):
        with mock.patch.object(dav, "_req", return_value=(500, b"err")):
            with self.assertRaises(SystemExit):
                dav._mkdirs(DAV_URL, "u", "p", "a")


class CmdTests(unittest.TestCase):
    def test_list_parses_multistatus(self):
        buf = io.StringIO()
        with mock.patch.object(dav, "_req", return_value=(207, PROPFIND_XML)), \
             mock.patch.object(sys, "stdout", buf):
            dav.cmd_list(_args(dir="Docs"))
        out = buf.getvalue()
        self.assertIn("dir ", out)
        self.assertIn("Docs", out)
        self.assertIn("report final.pdf", out)  # URL-decoded name
        self.assertIn("12345", out)

    def test_list_exits_on_non_207(self):
        with mock.patch.object(dav, "_req", return_value=(401, b"unauth")):
            with self.assertRaises(SystemExit):
                dav.cmd_list(_args(dir="Docs"))

    def test_upload_mkdirs_parent_and_puts(self):
        calls = []

        def fake_req(url, method, user, pw, **kw):
            calls.append((method, url))
            if method == "PUT":
                return 201, b""
            return 201, b""

        with tempfile_file("hello") as local, \
             mock.patch.object(dav, "_req", side_effect=fake_req), \
             mock.patch("builtins.print"):
            dav.cmd_upload(_args(local=str(local), remote="Docs/sub/hello.txt"))
        methods = [m for m, _ in calls]
        self.assertEqual(methods[-1], "PUT")
        self.assertTrue(calls[-1][1].endswith("/Docs/sub/hello.txt"))

    def test_download_writes_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "out.bin"
            with mock.patch.object(dav, "_req", return_value=(200, b"DATA")):
                dav.cmd_download(_args(remote="f.bin", local=str(local)))
            self.assertEqual(local.read_bytes(), b"DATA")

    def test_download_exits_on_error(self):
        with mock.patch.object(dav, "_req", return_value=(404, b"")):
            with self.assertRaises(SystemExit):
                dav.cmd_download(_args(remote="f", local="/tmp/x"))

    def test_move_sends_destination_header(self):
        seen = {}

        def fake_req(url, method, user, pw, headers=None, **kw):
            seen["headers"] = headers or {}
            seen["url"] = url
            return (204 if method == "MOVE" else 201), b""

        with mock.patch.object(dav, "_req", side_effect=fake_req), \
             mock.patch("builtins.print"):
            dav.cmd_move(_args(src="a.txt", dest="sub/b.txt"))
        self.assertEqual(seen["headers"]["Destination"], f"{DAV_URL}/sub/b.txt")

    def test_delete(self):
        with mock.patch.object(dav, "_req", return_value=(204, b"")) as m, \
             mock.patch("builtins.print"):
            dav.cmd_delete(_args(remote="gone.txt"))
        self.assertEqual(m.call_args[0][1], "DELETE")

    def test_share_posts_form_and_prints_url(self):
        body = (b'<?xml version="1.0"?><ocs><data>'
                b'<url>https://cloud.cloudless.gr/s/abc123</url>'
                b'<token>abc123</token></data></ocs>')
        seen = {}

        def fake_req(url, method, user, pw, data=None, headers=None, **kw):
            seen["url"] = url
            seen["data"] = data
            seen["headers"] = headers or {}
            return 200, body

        buf = io.StringIO()
        with mock.patch.object(dav, "_req", side_effect=fake_req), \
             mock.patch.object(sys, "stdout", buf):
            dav.cmd_share(_args(remote="Docs/x.pdf", password="pw1",
                                label="lbl", expire="2030-01-01"))
        self.assertIn("/ocs/v2.php/apps/files_sharing/api/v1/shares", seen["url"])
        form = urllib.parse.parse_qs(seen["data"].decode())
        self.assertEqual(form["path"], ["/Docs/x.pdf"])
        self.assertEqual(form["shareType"], ["3"])
        self.assertEqual(form["permissions"], ["1"])  # read-only — required
        self.assertEqual(form["password"], ["pw1"])
        self.assertEqual(form["label"], ["lbl"])
        self.assertEqual(form["expireDate"], ["2030-01-01"])
        self.assertEqual(seen["headers"]["OCS-APIRequest"], "true")
        self.assertIn("https://cloud.cloudless.gr/s/abc123", buf.getvalue())

    def test_share_exits_when_no_url_in_response(self):
        with mock.patch.object(dav, "_req", return_value=(200, b"<ocs></ocs>")):
            with self.assertRaises(SystemExit):
                dav.cmd_share(_args(remote="x"))

    def test_share_exits_on_http_error(self):
        with mock.patch.object(dav, "_req", return_value=(403, b"denied")):
            with self.assertRaises(SystemExit):
                dav.cmd_share(_args(remote="x"))


def tempfile_file(content: str):
    import tempfile
    td = tempfile.TemporaryDirectory()
    p = Path(td.name) / "local.txt"
    p.write_text(content)
    # Return a context manager yielding the path and cleaning up.
    class _Ctx:
        def __enter__(self_inner):
            return p

        def __exit__(self_inner, *a):
            td.cleanup()
            return False
    return _Ctx()


if __name__ == "__main__":
    unittest.main()
