"""Compatibility-surface checks between Ghost-ALICE core and this addon."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "compatibility-matrix.json"
REQUIRED_TARGETS = {
    "os-macos",
    "os-linux",
    "shell-windows-command-prompt",
    "shell-windows-powershell-5",
    "shell-windows-powershell-7",
    "agent-platform-claude",
    "agent-platform-codex",
}
ALLOWED_STATUSES = {"verified-local", "simulated-local", "not-run"}
TRACKED_BINARY_EXCLUSIONS = {
    "logo/logo_inward_fade.png",
    "logo/logo_outward_fade.png",
}
RELEASE_PACKAGE_FILES = (
    ".github/workflows/ci.yml",
    ".gitignore",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README_ko.md",
    "VERSION",
    "addons-manifest.json",
    "addons/autopilot-mode/addon.json",
    "addons/autopilot-mode/skill/SKILL.md",
    "addons/autopilot-mode/skill/adapters/autopilot_mode.py",
    "compatibility-matrix.json",
    "docs/release/2026-06-22-release-notes.md",
    "docs/release/2026-07-01-release-notes.md",
    "addons/autopilot-mode/skill/adapters/autopilot_messages.py",
    "addons/autopilot-mode/skill/adapters/autopilot_state.py",
    "addons/autopilot-mode/skill/adapters/autopilot_work_items.py",
    "addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py",
    "addons/autopilot-mode/skill/scripts/autopilot_session_bridge.py",
    "addons/autopilot-mode/skill/scripts/autopilot_session_material.py",
    "scripts/autopilot_session_bridge.py",
    "scripts/fresh_install_e2e.py",
    "scripts/live_semantic_e2e.py",
    "tests/test_autopilot_session_bridge.py",
    "tests/test_autopilot_messages.py",
    "tests/test_autopilot_state.py",
    "tests/test_compatibility_surface.py",
    "tests/test_governance_signal.py",
    "tests/test_live_semantic_e2e_harness.py",
    "tests/test_live_semantic_e2e_unittest.py",
    "tests/test_privileged_adapter.py",
)
BASH_FIRST_PUBLIC_DOC_PATTERNS = {
    "PowerShell code fence": re.compile(r"```powershell", re.IGNORECASE),
    "CMD code fence": re.compile(r"```cmd", re.IGNORECASE),
    "native Windows install wrapper execution": re.compile(
        r"(?m)(?:^\s*(?:\.\\)?install\.cmd(?:\s+|$)|`(?:\.\\)?install\.cmd\s+(?:-|--|\w))",
        re.IGNORECASE,
    ),
    "PowerShell installer execution": re.compile(
        r"(?m)(?:^\s*(?:\.\\)?install\.ps1(?:\s+|$)|`(?:\.\\)?install\.ps1\s+(?:-|--|\w))",
        re.IGNORECASE,
    ),
    "PowerShell-style installer flag": re.compile(
        r"(?<![A-Za-z0-9])-(?:Platform|Visibility|AgentVisibility|Uninstall|AddonSource|AddonTag|Status|Doctor|List|CleanupPending|PromptPlatform|Skills)\b"
    ),
}


def _scan_lines(rel: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in patterns.items():
            if pattern.search(line):
                findings.append(f"{rel}:{line_number}: {label}: {line.strip()}")
    return findings


def _compatibility_matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _addon_manifest() -> dict:
    return json.loads((REPO_ROOT / "addons" / "autopilot-mode" / "addon.json").read_text(encoding="utf-8"))


def _matrix_evidence(matrix: dict) -> list[str]:
    return [
        evidence
        for target in matrix["targets"]
        for evidence in target.get("evidence", [])
    ]


def _tracked_release_surface() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        rel
        for rel in result.stdout.splitlines()
        if rel not in TRACKED_BINARY_EXCLUSIONS and (REPO_ROOT / rel).is_file()
    )


class CompatibilitySurfaceTest(unittest.TestCase):
    def test_release_package_files_are_in_git_index(self) -> None:
        self.assertEqual(sorted(RELEASE_PACKAGE_FILES), _tracked_release_surface())
        for rel in RELEASE_PACKAGE_FILES:
            with self.subTest(rel=rel):
                self.assertTrue((REPO_ROOT / rel).is_file())
                result = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", rel],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_addon_manifest_version_matches_repo_version(self) -> None:
        self.assertEqual(
            (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            _addon_manifest()["addon_version"],
        )

    def test_compatibility_matrix_enumerates_required_targets(self) -> None:
        matrix = _compatibility_matrix()
        self.assertEqual(matrix["schema_version"], "autopilot-compatibility-matrix.v1")
        self.assertEqual(
            matrix["evidence_policy"],
            "Evidence entries describe the current support contract. Historical run timestamps belong in external test logs, CI artifacts, or release notes, not in this matrix.",
        )
        targets = matrix["targets"]
        target_ids = [target["id"] for target in targets]

        self.assertEqual(len(target_ids), len(set(target_ids)))
        self.assertTrue(REQUIRED_TARGETS.issubset(set(target_ids)))

        by_id = {target["id"]: target for target in targets}
        for target_id in REQUIRED_TARGETS:
            with self.subTest(target_id=target_id):
                target = by_id[target_id]
                self.assertIn(target["status"], ALLOWED_STATUSES)
                self.assertTrue(target["name"])
                self.assertIn(target["kind"], {"os", "shell", "agent-platform"})
                self.assertTrue(target["evidence"])
                self.assertIsInstance(target["full_compatibility_blocker"], bool)
                if target["status"] == "not-run":
                    self.assertIs(target["full_compatibility_blocker"], True)
                    self.assertTrue(target["next_evidence_required"])
                else:
                    self.assertIs(target["full_compatibility_blocker"], False)

    def test_matrix_evidence_is_not_a_dated_test_run_log(self) -> None:
        matrix = _compatibility_matrix()
        dated_run_pattern = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b|\bKST\b")

        for evidence in _matrix_evidence(matrix):
            with self.subTest(evidence=evidence):
                self.assertIsNone(dated_run_pattern.search(evidence))

    def test_claude_live_semantic_gap_stays_next_evidence_not_stale_evidence(self) -> None:
        matrix = _compatibility_matrix()
        by_id = {target["id"]: target for target in matrix["targets"]}
        claude = by_id["agent-platform-claude"]

        self.assertEqual(claude["status"], "simulated-local")
        self.assertIn("Claude live semantic E2E", " ".join(claude["next_evidence_required"]))
        evidence = "\n".join(claude["evidence"])
        self.assertNotIn("api_error_status", evidence)
        self.assertNotIn("401", evidence)

    def test_public_docs_surface_unverified_compatibility_targets(self) -> None:
        required_phrases = (
            "compatibility-matrix.json",
            "Windows Command Prompt",
            "PowerShell 5",
            "PowerShell 7",
            "Linux",
            "not-run",
        )
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in required_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_docs_explain_matrix_is_not_a_dated_test_log(self) -> None:
        expected = {
            "README.md": "not a chronological test log",
            "README_ko.md": "시간순 테스트 로그가 아니다",
            "addons/autopilot-mode/skill/SKILL.md": "not historical dated run prose",
        }
        for rel, phrase in expected.items():
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn(phrase, text)

    def test_user_facing_install_docs_do_not_reference_deleted_p6_branches(self) -> None:
        stale_refs = ("p6-privileged-adapter", "p6-autopilot")
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for stale in stale_refs:
                with self.subTest(rel=rel, stale=stale):
                    self.assertNotIn(stale, text)

    def test_public_docs_do_not_expose_internal_p6_phase_label(self) -> None:
        public_surfaces = (
            "README.md",
            "README_ko.md",
            "addons/autopilot-mode/skill/SKILL.md",
        )
        for rel in public_surfaces:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertNotIn("P6", text)

    def test_install_docs_lead_with_official_addon_alias(self) -> None:
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn("bash install.sh --addon autopilot", text)
                self.assertNotIn("--addon-source https://github.com/AidALL/ghost-alice-autopilot.git", text)

    def test_install_docs_state_commands_run_from_core_checkout(self) -> None:
        expected = {
            "README.md": "from a Ghost-ALICE core checkout",
            "README_ko.md": "Ghost-ALICE core checkout에서 실행한다",
            "addons/autopilot-mode/skill/SKILL.md": "from a Ghost-ALICE core checkout",
        }
        for rel, phrase in expected.items():
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn(phrase, text)

    def test_docs_do_not_reference_missing_demo_asset_placeholder(self) -> None:
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertNotIn("docs/demo/autopilot-mode.mp4", text)

    def test_install_docs_keep_codex_as_first_class_target(self) -> None:
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn("--platform codex --addon autopilot", text)
                self.assertNotIn("--platform claude --status", text)
                self.assertNotIn("--platform claude --uninstall --addon autopilot-mode", text)

    def test_core_repo_auto_discovery_uses_current_main_checkout_not_deleted_worktree(self) -> None:
        text = (REPO_ROOT / "tests" / "test_privileged_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn(".worktrees", text)
        self.assertNotIn("p6-autopilot", text)

    def test_user_docs_warn_old_core_can_install_inert_skill_without_adapter(self) -> None:
        english_expected = ("0.2.0", "inert", "without wiring the privileged adapter")
        for rel in ("README.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in english_expected:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)
        korean = (REPO_ROOT / "README_ko.md").read_text(encoding="utf-8")
        for phrase in ("0.2.0", "skill만 복사", "privileged adapter", "inert"):
            with self.subTest(rel="README_ko.md", phrase=phrase):
                self.assertIn(phrase, korean)

    def test_user_docs_require_before_stop_consistency_decision(self) -> None:
        required_phrases = ("before-stop", "consistency-decision.json", "reopen_micro", "reopen_macro")
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in required_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_docs_describe_governance_candidate_boundary(self) -> None:
        required_phrases = (
            "autopilot_governance_signal.py",
            "consistency-decision.candidate.json",
            "conduct-plan.candidate.json",
            "promotion",
            "adapter-consumable",
        )
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in required_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_governance_signal_script_is_part_of_public_addon_surface(self) -> None:
        required_scripts = (
            "autopilot_governance_signal.py",
            "autopilot_session_bridge.py",
        )
        for script_name in required_scripts:
            with self.subTest(script_name=script_name):
                script = (
                    REPO_ROOT
                    / "addons"
                    / "autopilot-mode"
                    / "skill"
                    / "scripts"
                    / script_name
                )
                self.assertTrue(script.is_file())

    def test_public_docs_list_release_adapter_helper_modules(self) -> None:
        required_files = (
            "skill/adapters/autopilot_messages.py",
            "skill/adapters/autopilot_work_items.py",
            "skill/scripts/autopilot_session_bridge.py",
            "skill/scripts/autopilot_session_material.py",
        )
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for required in required_files:
                with self.subTest(rel=rel, required=required):
                    self.assertIn(required, text)

    def test_docs_describe_session_intent_bridge_activation_path(self) -> None:
        required_phrases = (
            "autopilot_session_bridge.py",
            "current-session.json",
            "intent-state.json",
            "intent-events.jsonl",
            "approval evidence",
            "Codex",
            "Claude",
        )
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in required_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_codex_install_wording_matches_verified_matrix_evidence(self) -> None:
        matrix = _compatibility_matrix()
        by_id = {target["id"]: target for target in matrix["targets"]}
        codex = by_id["agent-platform-codex"]

        self.assertEqual(codex["status"], "verified-local")
        self.assertIs(codex["full_compatibility_blocker"], False)
        self.assertFalse(codex["next_evidence_required"])
        evidence = "\n".join(codex["evidence"])
        required_evidence_tokens = (
            "bash install.sh --platform codex --status",
            "content_hash-match",
            "scripts/live_semantic_e2e.py --runtime both --scenario-source intent --execute",
            "inference_status=ok",
            "semantic_status=parsed",
            "hook_status=complete",
            "action_file_allowed false",
            "direct promotion false",
        )
        for token in required_evidence_tokens:
            with self.subTest(token=token):
                self.assertIn(token, evidence)

        overconfident_phrases = (
            "Codex remains unverified",
            "Codex: `not-run`",
        )
        required_phrases = (
            "Codex: `verified-local`",
            "Codex live semantic E2E",
        )
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in overconfident_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertNotIn(phrase, text)
            for phrase in required_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_public_install_docs_use_bash_first_command_surface(self) -> None:
        findings: list[str] = []
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            findings.extend(_scan_lines(rel, BASH_FIRST_PUBLIC_DOC_PATTERNS))
        self.assertEqual([], findings)

if __name__ == "__main__":
    unittest.main()
