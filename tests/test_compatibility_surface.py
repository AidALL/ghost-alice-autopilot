"""Compatibility-surface checks between Ghost-ALICE core and this addon."""

from __future__ import annotations

import json
import re
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


def _top_level_addons_manifest() -> dict:
    return json.loads((REPO_ROOT / "addons-manifest.json").read_text(encoding="utf-8"))


def _matrix_evidence(matrix: dict) -> list[str]:
    return [
        evidence
        for target in matrix["targets"]
        for evidence in target.get("evidence", [])
    ]


class CompatibilitySurfaceTest(unittest.TestCase):
    def test_ci_uses_repo_local_project_test_runner(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("python -m pip install --upgrade pip pytest", workflow)
        self.assertIn("python scripts/run_project_tests.py tests", workflow)
        self.assertIn("GHOST_ALICE_CORE_ROOT: ${{ github.workspace }}/ghost-alice", workflow)
        self.assertNotIn("python -m unittest discover", workflow)


    def test_addon_manifest_version_matches_repo_version(self) -> None:
        self.assertEqual(
            (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            _addon_manifest()["addon_version"],
        )

    def test_addon_manifest_core_floor_matches_public_contract(self) -> None:
        self.assertEqual(_addon_manifest()["min_core_version"], "0.5.1")
        entry = _top_level_addons_manifest()["addons"][0]
        self.assertEqual(entry["id"], "autopilot-mode")
        self.assertEqual(entry["min_core_version"], "0.5.1")

    def test_compatibility_matrix_enumerates_required_targets(self) -> None:
        matrix = _compatibility_matrix()
        self.assertEqual(matrix["schema_version"], "autopilot-compatibility-matrix.v1")
        self.assertEqual(
            matrix["evidence_policy"],
            "Each status applies only to its recorded target and evidence scope. Source checks, native runtime validation, installed adoption and publication require their own evidence. Preserve each evidence record’s original source and verification time; an unchanged record does not certify a changed artifact.",
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


    def test_public_docs_link_target_evidence_and_preserve_blocker_rule(self) -> None:
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text()
            self.assertIn("compatibility-matrix.json", text)
        for rel, phrase in {"README.md": "untested targets remain unverified",
                            "README_ko.md": "검사하지 않은 대상은 미확인",
                            "addons/autopilot-mode/skill/SKILL.md": "A not-run target remains unverified"}.items():
            self.assertTrue(phrase in (REPO_ROOT / rel).read_text(), rel)
        self.assertTrue(any(t["status"] == "not-run" and t["full_compatibility_blocker"] for t in _compatibility_matrix()["targets"]))

    def test_docs_state_evidence_scope_for_compatibility(self) -> None:
        expected = {
            "README.md": "Source modification, checks, installation, native adoption and applied criteria are distinct observations",
            "README_ko.md": "소스 수정·검사·설치·네이티브 적용·기준 반영은 별개의 관찰",
            "addons/autopilot-mode/skill/SKILL.md": "source tests establish neither installed adoption nor live host behavior",
        }
        for rel, phrase in expected.items():
            self.assertTrue(phrase in (REPO_ROOT / rel).read_text(), rel)


    def test_install_docs_lead_with_official_addon_alias(self) -> None:
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                commands = re.findall(r"^bash install\.sh[^\n]*", text, re.MULTILINE)
                self.assertTrue(commands and "--addon autopilot" in commands[0], rel)
                self.assertNotIn("--addon-source https://github.com/AidALL/ghost-alice-autopilot.git", text)

    def test_install_docs_state_commands_run_from_core_checkout(self) -> None:
        expected = {
            "README.md": "Run the Core installer from the Core checkout",
            "README_ko.md": "Core 체크아웃에서 Core 설치기를 실행",
            "addons/autopilot-mode/skill/SKILL.md": "from the Ghost-ALICE Core checkout",
        }
        for rel, phrase in expected.items():
            self.assertTrue(phrase in (REPO_ROOT / rel).read_text(encoding="utf-8"), rel)


    def test_install_docs_keep_codex_as_first_class_target(self) -> None:
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertIn("--platform codex", text)
                self.assertIn("--addon autopilot", text)
                self.assertNotIn("--platform claude --status", text)
                self.assertNotIn("--platform claude --uninstall --addon autopilot-mode", text)


    def test_user_docs_state_current_core_floor_and_hook_requirement(self) -> None:
        floor = _addon_manifest()["min_core_version"]
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertTrue("Ghost-ALICE Core " + floor in text, rel)
                self.assertTrue("native hooks" in text if rel == "README.md" else "네이티브 훅" in text, rel)
                self.assertIn("--platform claude", text)
                self.assertIn("--platform codex", text)
                self.assertTrue("The Core installer registers the adapters" in text if rel == "README.md" else "Core 설치기는 어댑터를 등록" in text, rel)
        skill = (REPO_ROOT / "addons/autopilot-mode/skill/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Ghost-ALICE Core " + floor + "+", skill)
        self.assertIn("shared SQLite APIs", skill)

    def test_public_docs_do_not_describe_io_trace_as_bootstrap_condition(self) -> None:
        stale_phrases = (
            "session-intent plus io-trace or open conduct feedback",
            "session-intent plus io-trace/open conduct feedback",
            "session-intent와 io-trace 또는 open conduct feedback",
        )
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for phrase in stale_phrases:
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertNotIn(phrase, text)
            with self.subTest(rel=rel, contract="current-selection"):
                expected = {
                    "README.md": ("Core architecture", "authenticated review", "approved plan", "follow-up input"),
                    "README_ko.md": ("Core 아키텍처", "인증된 검토", "승인된 계획", "후속 입력"),
                    "addons/autopilot-mode/skill/SKILL.md": ("Ghost-ALICE Core completion service's publication_route", "Ghost-ALICE Autopilot adapter code does not classify final prose"),
                }
                for phrase in expected[rel]:
                    self.assertIn(phrase, text)
                self.assertNotIn("when at least one admitted unmet execution criterion exists", text)

    def test_managed_recording_and_approved_queue_have_scoped_contracts(self) -> None:
        skill = (REPO_ROOT / "addons/autopilot-mode/skill/SKILL.md").read_text()
        managed = skill.split("## Managed completion recording", 1)[1].split("## Consistency decisions", 1)[0]
        queue = skill.split("### Approved queue API", 1)[1].split("## Target and state inspection", 1)[0]
        for token in ("references/autopilot-publication.md", "Prepare and record the response review", "Native publication and manual CLI", "Recording-only failures", "Apply and deliver"):
            self.assertIn(token, managed)
        self.assertNotIn("--correction-json", managed)
        self.assertIn("references/continuation.md#register-the-approved-plan", skill)
        continuation = (REPO_ROOT / "addons/autopilot-mode/skill/references/continuation.md").read_text()
        for token in ("register_continuation_plan", "expected_input_event_id", "approval_evidence", "allowed_surfaces", "criterion_refs", "plan_id", "without requiring a completed response review or Stop application"):
            self.assertIn(token, continuation)
        for token in ("authorized objective", "queue admission or execution", "before-stop", "approval_generation", "promotion_evidence.decision", "promotion_evidence.source", "governance_signal_digest", "decision_key", "state_hash", "loop_key", "completion_check_digest", "array of strings", "other decisions require running"):
            self.assertIn(token, queue)
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text()
            self.assertIn("./addons/autopilot-mode/skill/SKILL.md#consistency-decisions", text)
            self.assertIn("./addons/autopilot-mode/skill/references/continuation.md#register-the-approved-plan", text)
            for token in ("prepare-response", "record-response", "response_text", "register_continuation_plan", "advance_continuation"):
                self.assertNotIn(token, text)

    def test_approved_queue_describes_candidate_boundary(self) -> None:
        skill = (REPO_ROOT / "addons/autopilot-mode/skill/SKILL.md").read_text()
        queue = skill.split("### Approved queue API", 1)[1]
        for token in ("autopilot_governance_signal.py", "consistency-decision.candidate.json", "conduct-plan.candidate.json", "observation_signal candidates remain diagnostic", "promotion_state: promoted", "promotion_state: approved"):
            self.assertIn(token, queue)

    def test_queue_promotion_retains_target_status_and_approval(self) -> None:
        skill = (REPO_ROOT / "addons/autopilot-mode/skill/SKILL.md").read_text()
        queue = skill.split("### Approved queue API", 1)[1]
        for token in ("continue_next accepts running/ready/reopened tasks", "other decisions require running", "approval_generation", "current-turn resolved action", "all schema and verification requirements still apply", "Final all-pass criterion-bound [completion-check]"):
            self.assertIn(token, queue)


    def test_public_repository_navigation_resolves_owning_modules(self) -> None:
        for name in ("autopilot_lineage.py", "autopilot_messages.py", "autopilot_work_items.py"):
            self.assertTrue((REPO_ROOT / "addons/autopilot-mode/skill/adapters" / name).is_file())
        for name in ("autopilot_session_bridge.py", "autopilot_session_material.py"):
            self.assertTrue((REPO_ROOT / "addons/autopilot-mode/skill/scripts" / name).is_file())
        for rel in ("README.md", "README_ko.md"):
            text = (REPO_ROOT / rel).read_text()
            for target in re.findall(r"\]\((\./addons/[^)]+)\)", text):
                path = target.split("#", 1)[0]
                self.assertTrue((REPO_ROOT / path).is_file(), target)
            self.assertIn("./addons/autopilot-mode/skill/SKILL.md#consistency-decisions", text)
            self.assertIn("./addons/autopilot-mode/skill/references/continuation.md#register-the-approved-plan", text)

    def test_queue_bridge_uses_exact_receipt_and_storage_authority(self) -> None:
        skill = (REPO_ROOT / "addons/autopilot-mode/skill/SKILL.md").read_text()
        for token in ("autopilot_session_bridge.py", "--intent-root", "--platform", "--session-id", "--run-dir", "--check", "explicit approval evidence", "nonempty source", "writes no run state", "does not redirect the host Stop target", "SQLite and public APIs are authoritative"):
            self.assertIn(token, skill)


    def test_public_install_docs_use_bash_first_command_surface(self) -> None:
        findings: list[str] = []
        for rel in ("README.md", "README_ko.md", "addons/autopilot-mode/skill/SKILL.md"):
            findings.extend(_scan_lines(rel, BASH_FIRST_PUBLIC_DOC_PATTERNS))
        self.assertEqual([], findings)

if __name__ == "__main__":
    unittest.main()
