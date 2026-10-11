import json
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


if __name__ == "__main__":
    unittest.main()
