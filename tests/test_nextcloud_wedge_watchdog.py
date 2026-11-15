"""Unit tests for .devin/skills/omv-nextcloud-ops/scripts/nextcloud_wedge_watchdog.py.

All docker/subprocess/ntfy I/O is stubbed — nothing touches the omv host.

    python3 -m unittest discover -s tests
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".devin/skills/omv-nextcloud-ops/scripts/nextcloud_wedge_watchdog.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("nc_wedge_watchdog", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nc_wedge_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod


wd = _load_module()


class _TmpState(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state_path = Path(self._td.name) / "state.json"
        self._patches = [
            mock.patch.object(wd, "STATE", self.state_path),
            # Keep the print() half of log() so main() output stays assertable;
            # only the syslog subprocess call is dropped.
            mock.patch.object(wd, "log",
                              lambda msg: print(msg, flush=True)),
            mock.patch.object(wd, "notify"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self):
        return json.loads(self.state_path.read_text())


class ProbeTests(_TmpState):
    def test_probe_ok_on_200(self):
        with mock.patch.object(wd, "sh", return_value=(0, "200\n")) as m:
            ok, code = wd.probe_app_route()
        self.assertTrue(ok)
        self.assertEqual(code, "200")
        self.assertIn("docker", m.call_args[0][0])
        self.assertIn("exec", m.call_args[0][0])

    def test_probe_fails_on_non_200(self):
        with mock.patch.object(wd, "sh", return_value=(0, "502\n")):
            ok, code = wd.probe_app_route()
        self.assertFalse(ok)
        self.assertEqual(code, "502")

    def test_probe_fails_on_rc_error(self):
        with mock.patch.object(wd, "sh", return_value=(1, "")):
            ok, code = wd.probe_app_route()
        self.assertFalse(ok)
        self.assertEqual(code, "000")

    def test_segfault_count_counts_marker(self):
        out = ("x\nexit signal Segmentation fault\ny\n"
               "exit signal Segmentation fault\nz")
        with mock.patch.object(wd, "sh", return_value=(0, out)):
            self.assertEqual(wd.segfault_count(), 2)

    def test_segfault_count_zero_on_error(self):
        with mock.patch.object(wd, "sh", return_value=(1, "exit signal Segmentation fault")):
            self.assertEqual(wd.segfault_count(), 0)


class MainTests(_TmpState):
    def _run(self, probe_ok=True, probe_code="200", segs=0, restart_rc=0,
             restart_out=""):
        """Drive main() with scripted sh() responses keyed by argv."""
        def fake_sh(cmd, timeout=15):
            if "exec" in cmd:  # probe_app_route
                return (0 if probe_ok else 0), f"{probe_code}\n"
            if "logs" in cmd:
                return (0, "exit signal Segmentation fault\n" * segs)
            if "restart" in cmd:
                return restart_rc, restart_out
            return 0, ""
        buf = io.StringIO()
        with mock.patch.object(wd, "sh", side_effect=fake_sh), \
             mock.patch.object(wd.time, "time", return_value=1_000_000.0), \
             redirect_stdout(buf):
            rc = wd.main()
        return rc, buf.getvalue()

    def test_healthy_run_saves_clean_state(self):
        rc, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._state()["fail_streak"], 0)

    def test_single_probe_failure_no_restart(self):
        wd.STATE.write_text(json.dumps({"fail_streak": 0, "last_restart": 0}))
        rc, _ = self._run(probe_ok=False, probe_code="502")
        self.assertEqual(rc, 0)  # streak=1 < FAIL_STREAK → not wedged
        self.assertEqual(self._state()["fail_streak"], 1)
        wd.notify.assert_not_called()

    def test_second_failure_restarts_container(self):
        wd.STATE.write_text(json.dumps({"fail_streak": 1, "last_restart": 0}))
        rc, out = self._run(probe_ok=False, probe_code="502")
        self.assertEqual(rc, 0)
        self.assertIn("restart complete", out)
        st = self._state()
        self.assertEqual(st["fail_streak"], 0)
        self.assertEqual(st["last_restart"], 1_000_000.0)
        wd.notify.assert_called_once()
        args = wd.notify.call_args[0]
        self.assertEqual(args[0], "Nextcloud auto-restarted")

    def test_segfault_storm_wedges_even_when_probe_ok(self):
        rc, _ = self._run(probe_ok=True, segs=wd.SEGFAULT_THRESHOLD)
        self.assertEqual(rc, 0)
        wd.notify.assert_called_once()
        self.assertIn("segfaults", wd.notify.call_args[0][1])

    def test_restart_cooldown_skips(self):
        recent = 1_000_000.0 - 60  # 1 min ago < 30 min cooldown
        wd.STATE.write_text(json.dumps({"fail_streak": 5, "last_restart": recent}))
        rc, out = self._run(probe_ok=False, probe_code="502")
        self.assertEqual(rc, 1)
        self.assertIn("cooldown", out)
        wd.notify.assert_not_called()

    def test_failed_restart_returns_2_and_pages(self):
        wd.STATE.write_text(json.dumps({"fail_streak": 1, "last_restart": 0}))
        rc, _ = self._run(probe_ok=False, probe_code="502",
                          restart_rc=1, restart_out="daemon gone")
        self.assertEqual(rc, 2)
        wd.notify.assert_called_once()
        self.assertEqual(wd.notify.call_args[0][0],
                         "Nextcloud wedge restart FAILED")
        self.assertEqual(wd.notify.call_args[0][2], "5")

    def test_recovery_notifies_after_fail_streak(self):
        wd.STATE.write_text(json.dumps({"fail_streak": wd.FAIL_STREAK,
                                        "last_restart": 0}))
        rc, _ = self._run(probe_ok=True)
        self.assertEqual(rc, 0)
        wd.notify.assert_called_once()
        self.assertEqual(wd.notify.call_args[0][0], "Nextcloud recovered")
        self.assertEqual(wd.notify.call_args[0][2], "3")


class HelperTests(unittest.TestCase):
    def test_load_state_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            with mock.patch.object(wd, "STATE", p):
                self.assertEqual(wd.load_state(),
                                 {"fail_streak": 0, "last_restart": 0})

    def test_load_state_corrupt_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("{not json")
            with mock.patch.object(wd, "STATE", p):
                self.assertEqual(wd.load_state(),
                                 {"fail_streak": 0, "last_restart": 0})

    def test_notify_never_raises(self):
        with mock.patch.object(wd.urllib.request, "urlopen",
                               side_effect=OSError("ntfy down")), \
             mock.patch.object(wd, "log") as mlog:
            wd.notify("t", "b")  # must not raise
        mlog.assert_called_once()
        self.assertIn("ntfy notify failed", mlog.call_args[0][0])

    def test_notify_sends_bearer_when_token_set(self):
        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            seen["req"] = req
            return _Resp()

        with mock.patch.object(wd, "NTFY_TOKEN", "tok-123"), \
             mock.patch.object(wd.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            wd.notify("Title X", "body", "5")
        req = seen["req"]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(req.get_header("Title"), "Title X")
        self.assertEqual(req.get_header("Priority"), "5")
        self.assertEqual(req.data, b"body")


if __name__ == "__main__":
    unittest.main()
