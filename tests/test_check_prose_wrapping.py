import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_prose_wrapping.py"


class ProseWrappingSmokeTest(unittest.TestCase):
    def run_checker(self, repo: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result, json.loads(result.stdout)

    def test_local_checker_rejects_adjacent_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            (repo / "README.md").write_text(
                "One ordinary sentence.\nAnother remains in the same paragraph.\n",
                encoding="utf-8",
            )
            result, payload = self.run_checker(repo)

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertEqual(payload["violation_count"], 1)

    def test_local_repository_is_clean(self) -> None:
        result, payload = self.run_checker(REPO_ROOT)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(payload["violation_count"], 0, payload["violations"])

    def test_vendored_checker_matches_core_when_available(self) -> None:
        core_root = os.environ.get("GHOST_ALICE_CORE_REPO")
        if not core_root:
            self.skipTest("GHOST_ALICE_CORE_REPO is not set")
        canonical = Path(core_root) / "scripts" / "check_prose_wrapping.py"
        if not canonical.is_file():
            self.skipTest(f"canonical checker is unavailable: {canonical}")

        self.assertEqual(SCRIPT.read_bytes(), canonical.read_bytes())


if __name__ == "__main__":
    unittest.main()
