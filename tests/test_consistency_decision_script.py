"""TDD for the addon-owned autopilot consistency decision writer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "scripts" / "autopilot_consistency_decision.py"


def _read_decision(run_dir: Path) -> dict:
    return json.loads((run_dir / "consistency-decision.json").read_text(encoding="utf-8"))


def _completion(verdict: str = "pass") -> str:
    return "\n".join([
        "[completion-check]",
        "- verification-before-completion: done",
        "- skill-call: verification-before-completion (this turn)",
        "- acceptance-criteria:",
        "  - unit-1: work item criterion [source: user-explicit]",
        "- claim-evidence-map:",
        "  - claim: work item handled",
        "    criterion: unit-1",
        "    evidence: pytest path",
        f"    verdict: {verdict}",
        "- unverified:",
        "  - none",
        "- evidence: pytest path",
        "",
        "[io-trace]",
        "- commands-run: [pytest path]",
        "- skills-loaded: [verification-before-completion]",
    ])


class ConsistencyDecisionScriptTest(unittest.TestCase):
    def test_passing_completion_writes_continue_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "--work-item-id", "unit-1"],
                input=_completion("pass"),
                text=True,
                capture_output=True,
                check=False,
            )
            decision = _read_decision(run_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decision["decision"], "continue_next")
        self.assertEqual(decision["work_item_id"], "unit-1")
        self.assertEqual(decision["verdict"], "pass")
        self.assertRegex(decision["completion_check_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_failing_completion_writes_retry_same_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "--work-item-id", "unit-1"],
                input=_completion("fail"),
                text=True,
                capture_output=True,
                check=False,
            )
            decision = _read_decision(run_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decision["decision"], "retry_same_unit")
        self.assertNotIn("verdict", decision)
        self.assertTrue(decision["evidence"])

    def test_missing_completion_check_writes_retry_same_unit_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "--work-item-id", "unit-1"],
                input="The work is complete.",
                text=True,
                capture_output=True,
                check=False,
            )
            decision = _read_decision(run_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decision["decision"], "retry_same_unit")
        self.assertIn("[completion-check]", decision["evidence"][0])

    def test_explicit_stop_requires_evidence_and_writes_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-dir",
                    str(run_dir),
                    "--work-item-id",
                    "unit-1",
                    "--decision",
                    "stop",
                    "--evidence",
                    "operator stop",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            decision = _read_decision(run_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decision["decision"], "stop")
        self.assertEqual(decision["evidence"], ["operator stop"])


if __name__ == "__main__":
    unittest.main()
