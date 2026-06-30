from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "live_semantic_e2e.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("live_semantic_e2e", HARNESS)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LiveSemanticE2EWindowsConfigTests(unittest.TestCase):
    def test_codex_config_failure_is_not_reported_as_parse_failure(self) -> None:
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "codex.log"
            last_message = root / "codex.txt"
            scenario = harness.LiveScenario(id="codex-config", prompt="Return compact JSON.")
            log.write_text(
                "Error loading config.toml: unknown variant `default`, expected `fast` or `flex`\n"
                "in `service_tier`\n",
                encoding="utf-8",
            )

            summary = harness.parse_codex_outputs(log, last_message, returncode=1, scenario=scenario)
            summary["scenario_id"] = scenario.id
            signal = harness.observation_signal_from_summary(summary, scenario=scenario)

        self.assertEqual(summary["inference_status"], "config-failed")
        self.assertEqual(summary["semantic_status"], "not-run")
        self.assertEqual(summary["missing_keys"], [])
        self.assertEqual(signal["classification"], "readiness-blocker")

    def test_codex_command_builder_prefers_cmd_and_conditional_hook_trust_on_windows(self) -> None:
        harness = _load_harness()
        scenario = harness.LiveScenario(id="probe", prompt="Return compact JSON.")

        def fake_which(name: str) -> str | None:
            return {
                "codex.cmd": r"C:\Users\try2q\AppData\Roaming\npm\codex.cmd",
                "codex.ps1": r"C:\Users\try2q\AppData\Roaming\npm\codex.ps1",
                "pwsh.exe": r"C:\Program Files\PowerShell\7\pwsh.exe",
                "codex.exe": r"C:\Program Files\Codex\codex.exe",
            }.get(name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = harness.build_codex_command(
                scenario,
                root / "codex.log",
                root / "codex.txt",
                hook_trust_supported=True,
                platform="nt",
                which=fake_which,
            )

        self.assertTrue(command[0].endswith("codex.cmd"), json.dumps(command))
        self.assertEqual(command[1], "exec")
        self.assertIn("--dangerously-bypass-hook-trust", command)

    def test_codex_command_builder_reads_prompt_from_stdin(self) -> None:
        harness = _load_harness()
        scenario = harness.LiveScenario(
            id="probe",
            prompt="First line.\nScenario text:\nClassify this scenario.",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = harness.build_codex_command(
                scenario,
                root / "codex.log",
                root / "codex.txt",
                codex_command=["codex"],
            )

        self.assertEqual(command[-1], "-")
        self.assertNotIn(scenario.prompt, command)


if __name__ == "__main__":
    unittest.main()
