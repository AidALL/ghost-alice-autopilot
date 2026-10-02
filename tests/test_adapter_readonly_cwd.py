"""Regression: the Stop adapter must not fail when the session CWD is read-only.

A Claude Code session started outside any project runs with CWD ``/``, which is
read-only on macOS. The adapter derived its run dir from that CWD and raised
``[Errno 30] Read-only file system: '/.autopilot'`` instead of degrading to a
no-op.

Run: /opt/homebrew/bin/python3 -m pytest tests/test_adapter_readonly_cwd.py -q
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_mode as apm  # noqa: E402
import autopilot_state as aps  # noqa: E402
import autopilot_storage as storage  # noqa: E402


class ReadOnlyCwdFallbackTest(unittest.TestCase):
    def test_selected_unwritable_project_is_preserved(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "GHOST_ALICE_PLATFORM": "claude"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(apm.os, "access", return_value=False):
                resolved = apm._env_with_hook_cwd({"cwd": project})
            self.assertEqual(resolved["GHOST_ALICE_AUTOPILOT_CWD"], project)

    def test_unexpected_derived_directory_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".autopilot"
            error = OSError(errno.ENOSPC, "No space left on device", str(root))
            with mock.patch.object(Path, "mkdir", side_effect=error):
                with self.assertRaises(OSError) as observed:
                    aps._bootstrap_then_advance(root, {}, Path(tmp), derived_run_dir=True)
            self.assertEqual(observed.exception.errno, errno.ENOSPC)

    def test_explicit_readonly_directory_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".autopilot"
            with mock.patch.object(Path, "mkdir", side_effect=OSError(errno.EROFS, "Read-only filesystem")):
                with self.assertRaises(OSError):
                    aps._bootstrap_then_advance(root, {}, Path(tmp), derived_run_dir=False)

    def test_env_with_hook_cwd_keeps_writable_cwd(self):
        """A writable CWD must still be used as-is."""
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "GHOST_ALICE_PLATFORM": "claude"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(apm.Path, "cwd", staticmethod(lambda: Path(project))):
                resolved = apm._env_with_hook_cwd({})
        self.assertEqual(resolved["GHOST_ALICE_AUTOPILOT_CWD"], project)

    def test_derived_run_dir_degrades_to_noop_on_read_only_filesystem(self):
        """EROFS on a derived run dir must degrade to a no-op, not raise."""
        real_mkdir = Path.mkdir

        def deny_rofs(self, *args, **kwargs):
            if self.name == ".autopilot":
                raise OSError(errno.EROFS, "Read-only file system", str(self))
            return real_mkdir(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "GHOST_ALICE_AUTOPILOT_CWD": "/"}
            with mock.patch.object(Path, "mkdir", deny_rofs):
                payload = aps.adapter_payload_from_env(env)
        self.assertEqual(payload, {"continue": True, "systemMessage": ""})

    def test_adapter_subprocess_from_root_cwd_reports_no_error(self):
        """End-to-end: running the real adapter from / must not emit an error payload."""
        with tempfile.TemporaryDirectory() as home:
            env = {
                "HOME": home,
                "PATH": os.environ.get("PATH", ""),
                "GHOST_ALICE_PLATFORM": "claude",
            }
            result = subprocess.run(
                [sys.executable, str(ADAPTER_DIR / "autopilot_mode.py")],
                input=json.dumps({"hook_event_name": "Stop"}),
                capture_output=True,
                text=True,
                cwd="/",
                env=env,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Read-only file system", result.stderr)
        payload = json.loads(result.stdout)
        self.assertNotIn("adapter error", payload.get("systemMessage", ""))


    def test_derived_acquisition_erofs_is_noop(self):
        with mock.patch.object(storage, "transaction") as transaction:
            transaction.return_value.__enter__.side_effect = OSError(errno.EROFS, "read-only")
            with storage.accessible_transaction("unused", {}, permission_denied_noop=True) as store:
                self.assertIsNone(store)

    def test_explicit_acquisition_erofs_remains_error(self):
        with mock.patch.object(storage, "transaction") as transaction:
            transaction.return_value.__enter__.side_effect = OSError(errno.EROFS, "read-only")
            with self.assertRaises(OSError) as caught:
                with storage.accessible_transaction("unused", {}):
                    self.fail("explicit target must not enter")
            self.assertEqual(caught.exception.errno, errno.EROFS)

    def test_unexpected_acquisition_error_remains_error(self):
        with mock.patch.object(storage, "transaction") as transaction:
            transaction.return_value.__enter__.side_effect = OSError(errno.EIO, "device error")
            with self.assertRaises(OSError) as caught:
                with storage.accessible_transaction("unused", {}, permission_denied_noop=True):
                    self.fail("unexpected error must not enter")
            self.assertEqual(caught.exception.errno, errno.EIO)

    def test_processing_erofs_remains_error(self):
        with mock.patch.object(storage, "transaction") as transaction:
            transaction.return_value.__exit__.return_value = False
            with self.assertRaises(OSError) as caught:
                with storage.accessible_transaction("unused", {}, permission_denied_noop=True):
                    raise OSError(errno.EROFS, "processing error")
            self.assertEqual(caught.exception.errno, errno.EROFS)


if __name__ == "__main__":
    unittest.main()
