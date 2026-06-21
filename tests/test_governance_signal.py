"""TDD for autopilot governance-signal candidates and promotion.

Run: /opt/homebrew/bin/python3 -m pytest tests/test_governance_signal.py -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "scripts" / "autopilot_governance_signal.py"


def _load_module(testcase: unittest.TestCase):
    testcase.assertTrue(SCRIPT_PATH.is_file(), f"{SCRIPT_PATH} must exist")
    spec = importlib.util.spec_from_file_location("autopilot_governance_signal_under_test", SCRIPT_PATH)
    testcase.assertIsNotNone(spec)
    testcase.assertIsNotNone(spec.loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _intent_state_with_conduct_feedback(*, occurrence_count: int = 2) -> dict:
    return {
        "schema_version": "session-intent-ledger.v1",
        "current_goal": "implement approved autopilot behavior upgrade",
        "conduct_feedback": [
            {
                "id": "scope-drift",
                "status": "open",
                "summary": "agent narrowed or reported instead of executing the approved implementation",
                "corrective_rule": "reopen the work boundary when execution diverges from approved scope",
                "occurrence_count": occurrence_count,
            }
        ],
    }


class GovernanceSignalTest(unittest.TestCase):
    def test_conduct_feedback_scope_drift_becomes_macro_reopen_candidate_not_action_file(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            intent_state=_intent_state_with_conduct_feedback(),
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["schema_version"], "autopilot-consistency-decision-candidate.v1")
        self.assertEqual(candidate["promotion_state"], "candidate")
        self.assertIs(candidate["action_file_allowed"], False)
        self.assertEqual(candidate["work_item_id"], "work-1")
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertTrue(candidate["evidence_digest"].startswith("sha256:"))
        self.assertTrue(candidate["state_hash"].startswith("sha256:"))
        self.assertTrue(candidate["decision_key"])
        self.assertIn("loop_key", candidate["loop_guard"])
        self.assertIn("conduct_feedback:scope-drift", "\n".join(candidate["evidence"]))

    def test_no_evidence_returns_no_decision_candidate_instead_of_retry(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            intent_state={"schema_version": "session-intent-ledger.v1"},
        )

        self.assertIsNone(candidate)

    def test_invalid_completion_validation_becomes_retry_candidate(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            completion_validation={
                "valid": False,
                "errors": ["missing claim-evidence-map"],
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "retry_same_unit")
        self.assertIn("completion_validation:missing claim-evidence-map", "\n".join(candidate["evidence"]))

    def test_routing_surface_correction_reopens_macro(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            routing_surface={
                "intent_relation": "correction",
                "focus_layer": "macro",
                "reason": "large premise mismatch in approved work",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertIn("routing_surface:correction", "\n".join(candidate["evidence"]))

    def test_live_observation_tool_loop_timeout_reopens_meso_candidate_not_action_file(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "signal_id": "observation-codex-tool-loop-tool-loop-timeout",
                "runtime": "codex",
                "scenario_id": "tool-loop",
                "classification": "tool-loop-timeout",
                "inference_status": "timeout",
                "semantic_status": "not-run",
                "hook_status": "not-applicable",
                "agent_activity": "tool-loop-timeout",
                "focus_layer": "meso",
                "loop_guard": "tool-loop-timeout",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_meso")
        self.assertEqual(candidate["source"], "observation_signal")
        self.assertIs(candidate["action_file_allowed"], False)
        self.assertIn("observation_signal:tool-loop-timeout", "\n".join(candidate["evidence"]))
        self.assertIn("observation_runtime:codex", "\n".join(candidate["evidence"]))

    def test_auth_failed_observation_does_not_create_semantic_reopen_candidate(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "claude",
                "scenario_id": "auth",
                "classification": "readiness-blocker",
                "inference_status": "auth-failed",
                "semantic_status": "not-run",
                "hook_status": "not-applicable",
            },
        )

        self.assertIsNone(candidate)

    def test_semantic_observation_mismatch_with_composite_focus_reopens_macro_candidate(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "observer-default-install-scope-gap",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "tool-using",
                "mismatch_detected": True,
                "focus_layer": "meso->macro",
                "next_action": "Reopen observer contract around cross-platform install semantics.",
                "loop_guard": "Block completion claims until evidence covers default install plus both supported platforms.",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertEqual(candidate["source"], "observation_signal")
        self.assertIs(candidate["action_file_allowed"], False)
        self.assertIn("observation_signal:semantic-observation", "\n".join(candidate["evidence"]))
        self.assertIn("observation_focus_layer:meso->macro", "\n".join(candidate["evidence"]))

    def test_semantic_observation_request_verification_reopens_without_mismatch_flag(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "unverified-endorsement",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "verdict": "request_verification",
                "mismatch_detected": False,
                "focus_layer": "verification-boundary",
                "next_action": "request source-backed verification before side effects",
                "loop_guard": "stop before completion claims without evidence",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertEqual(candidate["source"], "observation_signal")
        self.assertIs(candidate["action_file_allowed"], False)
        evidence = "\n".join(candidate["evidence"])
        self.assertIn("observation_verdict:request_verification", evidence)
        self.assertIn("observation_next_action:request source-backed verification before side effects", evidence)
        self.assertIn("observation_loop_guard:stop before completion claims without evidence", evidence)

    def test_semantic_observation_stale_conflict_verification_wording_reopens_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "stale-conflict",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "verdict": "needs_review",
                "mismatch_detected": False,
                "focus_layer": "meso",
                "next_action": "resolve stale/conflicting intent with source-backed verification before continuing",
                "loop_guard": "do not pass through stale plan artifacts",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_meso")
        evidence = "\n".join(candidate["evidence"])
        self.assertIn("observation_verdict:needs_review", evidence)
        self.assertIn("observation_next_action:resolve stale/conflicting intent", evidence)
        self.assertIn("observation_loop_guard:do not pass through stale plan artifacts", evidence)

    def test_semantic_observation_candidate_is_not_promotable_to_action(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "semantic-observation",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "macro",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["source"], "observation_signal")
        self.assertIs(candidate["action_file_allowed"], False)
        self.assertIsNone(module.promote_candidate_to_action(candidate))

    def test_cli_refuses_to_promote_semantic_observation_candidate(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "semantic-observation",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "macro",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_path = root / "consistency-decision.candidate.json"
            action_path = root / "consistency-decision.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "promote-decision",
                    "--candidate",
                    str(candidate_path),
                    "--out",
                    str(action_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            action_exists = action_path.exists()

        self.assertEqual(result.returncode, 3)
        self.assertFalse(action_exists)

    def test_semantic_observation_mismatch_with_underscore_focus_path_reopens_last_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "unverified-endorsement-of-review-items",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "tool-using",
                "mismatch_detected": True,
                "focus_layer": "meta_to_meso_to_micro",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_micro")
        self.assertIn("observation_focus_layer:meta_to_meso_to_micro", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_slash_focus_path_reopens_last_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "cf6",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "macro/meta",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "ask_user_meta")
        self.assertIn("observation_focus_layer:macro/meta", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_unicode_arrow_focus_path_reopens_last_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "backend-serialized-io-contract",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "macro→meso: adapter design must move to backend I/O contract checks",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_meso")
        self.assertIn("observation_focus_layer:macro→meso", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_leading_focus_descriptor_reopens_named_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "macro-strategy-with-meso-evaluator-followup",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "macro_strategy_with_meso_evaluator_followup",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertIn("observation_focus_layer:macro_strategy", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_domain_focus_fails_closed_to_macro(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "do-not-invent-legal-blockers",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "verification_and_boundary",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_macro")
        self.assertIn("observation_focus_layer:verification_and_boundary", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_non_string_focus_uses_default(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "non-string-focus",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": ["meso"],
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_meso")
        self.assertNotIn("observation_focus_layer:", "\n".join(candidate["evidence"]))

    def test_semantic_observation_mismatch_with_slash_domain_keeps_known_focus(self) -> None:
        module = _load_module(self)

        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            governance_signal={
                "schema_version": "autopilot-observation-signal.v1",
                "source": "live_semantic_e2e",
                "runtime": "codex",
                "scenario_id": "single-room-context",
                "classification": "semantic-observation",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
                "agent_activity": "direct-answer",
                "mismatch_detected": True,
                "focus_layer": "meso/document-plan",
            },
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["decision"], "reopen_meso")
        self.assertIn("observation_focus_layer:meso/document-plan", "\n".join(candidate["evidence"]))

    def test_promotion_requires_evidence_and_preserves_candidate_boundary(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            completion_validation={"valid": False, "errors": ["tests failed"]},
        )

        action = module.promote_candidate_to_action(candidate)

        self.assertEqual(action["work_item_id"], "work-1")
        self.assertEqual(action["decision"], "retry_same_unit")
        self.assertIn("candidate_id", action)
        self.assertEqual(action["promotion_state"], "promoted")
        self.assertTrue(action["promotion_evidence"])
        self.assertIsNone(module.promote_candidate_to_action(None))

        bad_candidate = dict(candidate)
        bad_candidate["evidence"] = []
        self.assertIsNone(module.promote_candidate_to_action(bad_candidate))

    def test_promotion_escalates_repeated_same_state_to_user_meta(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            intent_state=_intent_state_with_conduct_feedback(),
        )
        loop_key = candidate["loop_guard"]["loop_key"]

        action = module.promote_candidate_to_action(candidate, prior_loop_keys=[loop_key])

        self.assertEqual(action["decision"], "ask_user_meta")
        self.assertIn("loop-guard: repeated decision/state", "\n".join(action["evidence"]))

    def test_promotion_escalates_retry_attempt_cap_to_user_meta(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            completion_validation={"valid": False, "errors": ["tests failed"]},
        )

        action = module.promote_candidate_to_action(candidate, current_attempt=2)

        self.assertEqual(action["decision"], "ask_user_meta")
        self.assertIn("loop-guard: retry attempt cap", "\n".join(action["evidence"]))

    def test_cli_promotion_reads_run_dir_attempt_cap(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            completion_validation={"valid": False, "errors": ["tests failed"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".autopilot"
            run_dir.mkdir()
            candidate_path = run_dir / "consistency-decision.candidate.json"
            action_path = run_dir / "consistency-decision.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            (run_dir / "tasks.jsonl").write_text(
                json.dumps({
                    "id": "work-1",
                    "status": "running",
                    "attempt": 2,
                })
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "promote-decision",
                    "--candidate",
                    str(candidate_path),
                    "--run-dir",
                    str(run_dir),
                    "--out",
                    str(action_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            action = json.loads(action_path.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(action["decision"], "ask_user_meta")
        self.assertIn("loop-guard: retry attempt cap", "\n".join(action["evidence"]))

    def test_cli_promotion_reads_run_dir_prior_loop_key(self) -> None:
        module = _load_module(self)
        candidate = module.decision_candidate_from_governance(
            work_item_id="work-1",
            intent_state=_intent_state_with_conduct_feedback(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".autopilot"
            run_dir.mkdir()
            candidate_path = run_dir / "consistency-decision.candidate.json"
            action_path = run_dir / "consistency-decision.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "event": "consistency_decision_applied",
                    "work_item_id": "work-1",
                    "loop_key": candidate["loop_guard"]["loop_key"],
                })
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "promote-decision",
                    "--candidate",
                    str(candidate_path),
                    "--run-dir",
                    str(run_dir),
                    "--out",
                    str(action_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            action = json.loads(action_path.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(action["decision"], "ask_user_meta")
        self.assertIn("loop-guard: repeated decision/state", "\n".join(action["evidence"]))

    def test_semantic_delta_starvation_creates_conduct_plan_candidate(self) -> None:
        module = _load_module(self)

        candidate = module.conduct_plan_candidate_from_governance(
            intent_state=_intent_state_with_conduct_feedback(occurrence_count=3),
            current_work_item_id="work-1",
            plan_path=".tmp/implementation-plans/2026-06-21-autopilot-behavior-upgrade.md",
        )

        self.assertEqual(candidate["schema_version"], "autopilot-conduct-plan-candidate.v1")
        self.assertEqual(candidate["promotion_state"], "candidate")
        self.assertIs(candidate["action_file_allowed"], False)
        plan = candidate["conduct_plan"]
        proposal = plan["proposed_queue_items"][0]
        self.assertEqual(plan["schema_version"], "autopilot-conduct-plan.v2")
        self.assertEqual(proposal["proposal_status"], "proposed")
        self.assertIs(proposal["approval_required"], True)
        self.assertIs(proposal["observer_agent_required"], True)
        self.assertEqual(proposal["observer_contract"]["mode"], "read_only")
        self.assertIn(
            ".tmp/implementation-plans/2026-06-21-autopilot-behavior-upgrade.md",
            proposal["task_template"]["allowed_surface"],
        )

    def test_conduct_plan_candidate_requires_explicit_promotion_before_adapter_import(self) -> None:
        module = _load_module(self)
        candidate = module.conduct_plan_candidate_from_governance(
            intent_state=_intent_state_with_conduct_feedback(occurrence_count=3),
            current_work_item_id="work-1",
            plan_path=".tmp/implementation-plans/2026-06-21-autopilot-behavior-upgrade.md",
        )

        approved = module.promote_conduct_plan_candidate(
            candidate,
            approval_evidence={"decision": "GO", "source": "unit-test"},
        )

        self.assertEqual(approved["schema_version"], "autopilot-conduct-plan.v2")
        self.assertEqual(approved["promotion_state"], "approved")
        self.assertEqual(approved["source_candidate_id"], candidate["candidate_id"])
        self.assertEqual(approved["evidence_digest"], candidate["evidence_digest"])
        self.assertEqual(approved["approval_evidence"]["decision"], "GO")

    def test_cli_writes_candidate_then_promotes_action_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_state = root / "intent-state.json"
            candidate_path = root / ".autopilot" / "consistency-decision.candidate.json"
            action_path = root / ".autopilot" / "consistency-decision.json"
            intent_state.write_text(json.dumps(_intent_state_with_conduct_feedback()), encoding="utf-8")

            candidate_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "decision-candidate",
                    "--work-item-id",
                    "work-1",
                    "--intent-state",
                    str(intent_state),
                    "--out",
                    str(candidate_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            promote_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "promote-decision",
                    "--candidate",
                    str(candidate_path),
                    "--out",
                    str(action_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            action = json.loads(action_path.read_text(encoding="utf-8"))

        self.assertEqual(candidate_result.returncode, 0, candidate_result.stderr)
        self.assertEqual(promote_result.returncode, 0, promote_result.stderr)
        self.assertEqual(candidate["promotion_state"], "candidate")
        self.assertIs(candidate["action_file_allowed"], False)
        self.assertEqual(action["decision"], "reopen_macro")
        self.assertEqual(action["work_item_id"], "work-1")


if __name__ == "__main__":
    unittest.main()
