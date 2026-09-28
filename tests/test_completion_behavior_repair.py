"""Regressions for completion evidence, admission projection, and recovery guidance.

Exercises production adapter paths; no model or installed-runtime execution.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "addons/autopilot-mode/skill"
sys.path.insert(0, str(SKILL / "adapters"))
sys.path.insert(0, str(SKILL / "scripts"))
import autopilot_messages as messages
import autopilot_work_items as work_items
import autopilot_session_material as material
import autopilot_governance_signal as governance

DIGEST = "sha256:" + "a" * 64
VALID = """[completion-check]
- acceptance-criteria:
  - report: Apply the approved report change [source: user-explicit]
- claim-evidence-map:
  - claim: The report contains the approved line
    criterion: report
    evidence: readback compared the exact bytes
    verdict: pass
- unverified:
  - none
"""


def item():
    return {"id": "report", "status": "running", "focus_layer": "micro",
            "prompt": "Update the report and retain its backup", "acceptance_criteria": ["report"],
            "allowed_surface": ["personal/report.md"], "completion": {}}


def complete(text):
    return work_items.apply_consistency_decision(
        [item()], "report", "continue_next", completion_check_digest=DIGEST,
        verdict="pass", evidence=[text],
    )


def core_validator():
    root = Path(os.environ["GHOST_ALICE_CORE_ROOT"])
    path = root / "_shared/completion_check_validator.py"
    spec = importlib.util.spec_from_file_location("repair_core_completion_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompletionFormatParityTest(unittest.TestCase):
    def assert_rejected(self, text):
        self.assertIsNotNone(core_validator().validate_completion_evidence_map(text))
        with self.assertRaises(work_items.AutopilotStateError):
            complete(text)

    def test_inline_none_is_valid_on_actual_completion_transition(self):
        text = VALID.replace("- unverified:\n  - none", "- unverified: none")
        self.assertIsNone(core_validator().validate_completion_evidence_map(text))
        self.assertEqual(complete(text)[0]["status"], "completed")

    def test_explicit_semicolon_fields_are_valid(self):
        text = VALID.replace(
            "  - claim: The report contains the approved line\n    criterion: report\n"
            "    evidence: readback compared the exact bytes\n    verdict: pass",
            "  - claim: The report contains the approved line; criterion: report; "
            "evidence: readback compared the exact bytes; verdict: pass",
        )
        self.assertIsNone(core_validator().validate_completion_evidence_map(text))
        self.assertEqual(complete(text)[0]["status"], "completed")

    def test_semicolon_criterion_delimiter_preserves_multiple_bindings(self):
        text = VALID.replace("- claim-evidence-map:", "  - backup: Original retained [source: user-explicit]\n- claim-evidence-map:")
        text = text.replace("criterion: report", "criterion: report; backup")
        self.assertIsNone(core_validator().validate_completion_evidence_map(text))
        self.assertEqual(complete(text)[0]["status"], "completed")

    def test_inline_pending_cannot_be_hidden_by_nested_none(self):
        self.assert_rejected(VALID.replace("- unverified:", "- unverified: pending check"))

    def test_unbulleted_pending_cannot_be_hidden_by_nested_none(self):
        self.assert_rejected(VALID.replace("  - none", "  pending check\n  - none"))

    def test_duplicate_unverified_section_is_rejected(self):
        self.assert_rejected(VALID + "- unverified: pending check\n")

    def test_missing_evidence_is_rejected(self):
        self.assert_rejected(VALID.replace("    evidence: readback compared the exact bytes\n", ""))

    def test_duplicate_verdict_cannot_overwrite_fail_with_pass(self):
        self.assert_rejected(VALID.replace("    verdict: pass", "    verdict: fail\n    verdict: pass"))

    def test_duplicate_evidence_is_rejected(self):
        self.assert_rejected(VALID.replace("    verdict: pass", "    evidence: different readback\n    verdict: pass"))

    def test_malformed_second_entry_is_rejected(self):
        self.assert_rejected(VALID.replace("- unverified:", "  - report: missing claim\n- unverified:"))

    def test_claim_first_and_all_pass_remain_required(self):
        with self.assertRaises(work_items.AutopilotStateError):
            complete(VALID.replace("verdict: pass", "verdict: fail"))
        self.assert_rejected(VALID.replace("  - claim: The report contains the approved line\n    criterion: report", "  - criterion: report\n    claim: The report contains the approved line"))

    def test_quoted_fake_verdict_remains_evidence(self):
        for quote in ('"', "'", '`'):
            text = VALID.replace("evidence: readback compared the exact bytes", f"evidence: {quote}echo ; verdict: fail{quote}")
            self.assertEqual(complete(text)[0]["status"], "completed")

    def test_standalone_transition_does_not_require_core(self):
        # The completion path itself uses no core runtime; only this test's parity oracle does.
        env = dict(os.environ)
        env.pop("GHOST_ALICE_CORE_ROOT", None)
        code = "import sys; sys.path.insert(0, sys.argv[1]); import autopilot_work_items as w; w._validate_continue_next_evidence(sys.argv[2], [sys.argv[3]])"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(SKILL / "adapters"), DIGEST, VALID.replace("- unverified:\n  - none", "- unverified: none")], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_additional_invalid_sections_and_empty_values_reject(self):
        for text in (
            VALID + "- acceptance-criteria:\n  - other: conflicting scope\n",
            VALID + "- claim-evidence-map:\n",
            VALID.replace("  - none", ""),
            VALID.replace("- unverified:\n  - none", "- unverified: none\n  - pending readback"),
            VALID.replace("    verdict: pass", "    verdict: fail; verdict: pass"),
        ):
            with self.subTest(text=text):
                self.assert_rejected(text)

    def test_core_compact_proof_remains_insufficient_for_ap_binding(self):
        text = VALID.replace("- acceptance-criteria:\n  - report: Apply the approved report change [source: user-explicit]\n", "")
        self.assertIsNone(core_validator().validate_completion_evidence_map(text))
        with self.assertRaises(work_items.AutopilotStateError):
            complete(text)

    def test_standalone_negative_matrix_matches_transition_validation(self):
        cases = [
            VALID.replace("    evidence: readback compared the exact bytes\n", ""),
            VALID.replace("    verdict: pass", "    verdict: fail\n    verdict: pass"),
            VALID.replace("- unverified:", "- unverified: pending check"),
            VALID + "- unverified: pending check\n",
        ]
        env = dict(os.environ)
        env.pop("GHOST_ALICE_CORE_ROOT", None)
        code = "import sys,json; sys.path.insert(0,sys.argv[1]); import autopilot_work_items as w\nfor text in json.loads(sys.stdin.read()):\n try: w._validate_continue_next_evidence(sys.argv[2],[text])\n except w.AutopilotStateError: continue\n raise SystemExit('invalid standalone proof accepted')"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(SKILL / "adapters"), DIGEST], input=json.dumps(cases), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class BoundCompletionConsumerTest(unittest.TestCase):
    def submit(self, text, *, accepted):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_autopilot_bridge_admission as fixtures
        import autopilot_state as state_adapter
        helper = fixtures.BridgeAdmissionBoundaryTest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = helper.setup_ledger(root)
            state = json.loads(state_path.read_text())
            state["acceptance_criteria"] = [
                {"id": value, "summary": value, "source": "user-explicit", "admitted": True, "status": "unmet"}
                for value in ("report", "backup")
            ]
            state_path.write_text(json.dumps(state))
            bridge = helper.invoke(root)
            self.assertEqual(bridge.returncode, 0, bridge.stderr)
            run_dir = root / "external-run"
            run = state_adapter.storage.read(run_dir / "approved-run.json")
            task = state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]
            decision = fixtures._decision_action(task["id"], "continue_next", verdict="pass", completion_check_digest=DIGEST,
                evidence=[text], approval_generation=run["approval_generation"])
            (run_dir / "consistency-decision.json").write_text(json.dumps(decision))
            env = {"GHOST_ALICE_CORE_ROOT": os.environ["GHOST_ALICE_CORE_ROOT"], "GHOST_ALICE_PLATFORM": "codex",
                   "GHOST_ALICE_SESSION_ID": "session-1", "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent"), "PWD": str(root)}
            if accepted:
                state_adapter.advance_approved_run(run_dir, env)
            else:
                with self.assertRaises(work_items.AutopilotStateError):
                    state_adapter.advance_approved_run(run_dir, env)
            after_task = state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]
            after_state = fixtures.read_authority(state_path)
            self.assertEqual(after_task["status"], "completed" if accepted else task["status"])
            self.assertEqual({row["id"]: row["status"] for row in after_state["acceptance_criteria"]},
                             {"report": "met" if accepted else "unmet", "backup": "met" if accepted else "unmet"})

    def test_bound_scalar_and_semicolon_evidence_materialize_same_two_ids(self):
        text = VALID.replace("- claim-evidence-map:", "  - backup: Backup preserved [source: user-explicit]\n- claim-evidence-map:")
        text = text.replace("criterion: report", "criterion: report; backup")
        text = text.replace("- unverified:\n  - none", "- unverified: none")
        self.submit(text, accepted=True)

    def test_bound_invalid_proof_does_not_complete_task_or_mark_criteria(self):
        invalid = (
            VALID.replace("    evidence: readback compared the exact bytes\n", ""),
            VALID.replace("    verdict: pass", "    verdict: fail\n    verdict: pass"),
            VALID.replace("- unverified:", "- unverified: pending check"),
            VALID + "- unverified: pending check\n",
        )
        for text in invalid:
            with self.subTest(text=text):
                self.submit(text, accepted=False)


class IntentProjectionTest(unittest.TestCase):
    def state(self):
        return {"current_goal": "Publish the current personal report to shared, retaining personal files",
                "acceptance_criteria": [
                    {"id": "publish", "summary": "Shared has the approved current report", "source": "user-explicit", "admitted": True, "status": "unmet"},
                    {"id": "personal", "summary": "Preserve personal files", "source": "inferred", "admitted": False, "status": "unmet"},
                ],
                "constraints": ["Preserve all existing personal edits"],
                "non_goals": ["Do not change the shared report"],
                "decisions": [{"id": "exception", "summary": "Explicit current exception permits publishing the shared report", "superseded": False},
                              {"id": "old", "summary": "Edit personal only", "superseded": True}]}

    def task(self, state):
        return material.session_intent_task(intent_state=state, session_id="current", allowed_surfaces=["shared/report.md"])

    def test_only_admitted_criteria_are_completion_obligations(self):
        task = self.task(self.state())
        self.assertEqual(len(task["acceptance_criteria"]), 1)
        self.assertIn("publish:", task["acceptance_criteria"][0])
        self.assertIn("source: user-explicit", task["acceptance_criteria"][0])
        self.assertIn("admitted: true", task["acceptance_criteria"][0])

    def test_unadmitted_protection_is_visible_with_provenance(self):
        task = self.task(self.state())
        rendered = messages.build_continuation_message({"run_id": "r"}, task)
        self.assertIn("contextual-protections:", rendered)
        protection = next(line for line in rendered.splitlines() if "personal:" in line)
        self.assertIn("source: inferred", protection)
        self.assertIn("admitted: false", protection)
        self.assertIn("auxiliary evidence", rendered)

    def test_context_retains_constraints_without_reviving_superseded_scope(self):
        state = self.state()
        original = copy.deepcopy(state)
        rendered = messages.build_continuation_message({"run_id": "r"}, self.task(state))
        self.assertIn("Preserve all existing personal edits", rendered)
        self.assertIn("Do not change the shared report", rendered)
        self.assertIn("Explicit current exception permits publishing", rendered)
        self.assertNotIn("Edit personal only", rendered)
        self.assertIn("newer explicit decisions", rendered)
        self.assertIn("do not revive superseded scope", rendered)
        self.assertEqual(state, original)

    def test_no_admitted_criteria_does_not_invent_completion_obligation(self):
        state = self.state()
        state["acceptance_criteria"] = state["acceptance_criteria"][1:]
        self.assertEqual(self.task(state)["acceptance_criteria"], [])

    def test_approval_snapshot_keeps_every_criterion_definition(self):
        state = self.state()
        source = {"platform": "codex", "session_id": "current", "state_path": Path("/snapshot/state"), "events_path": Path("/snapshot/events"), "events": [], "latest_input_event": {}, "intent_state": state}
        evidence = material.session_evidence(source)
        self.assertEqual(evidence["acceptance_criteria"], state["acceptance_criteria"])
        self.assertIsNot(evidence["acceptance_criteria"], state["acceptance_criteria"])


class ContinuationRecoveryTest(unittest.TestCase):
    def assert_recovery_proof_handoff(self, text):
        # Contract placement only: these assertions do not measure model compliance.
        obligations = (
            "If this replacement final asserts business completion",
            "include the supported [completion-check] in the final answer as well as in runtime action evidence",
            "criteria, artifacts, and evidence remain unchanged and valid",
            "original source and verification time",
            "do not describe an earlier check as freshly run",
            "runtime records or final formatting",
            "Verify changed runtime records separately",
            "do not refresh business proof",
            "do not copy a prior turn's skill-call as a current-turn load",
            "reopen only the affected verification",
            "without a finalized [completion-check]",
        )
        self.assertEqual([obligation for obligation in obligations if obligation not in text], [])

    def test_continuation_places_conditional_proof_handoff_in_recovery_section(self):
        text = messages.build_continuation_message({"run_id": "r"}, item())
        recovery = text.split("recovery-and-final-answer:\n", 1)[1].split("\nprompt:", 1)[0]
        self.assert_recovery_proof_handoff(recovery)

    def test_observation_candidate_is_not_suggested_for_promotion(self):
        candidate = {"schema_version": "autopilot-consistency-decision-candidate.v1", "promotion_state": "candidate", "action_file_allowed": False, "candidate_id": "seen", "source": "observation_signal", "work_item_id": "report", "decision": "reopen_micro", "evidence": ["readback observed"]}
        self.assertIsNone(governance.promote_candidate_to_action(candidate, work_item_status="running"))
        text = messages.build_continuation_message({"run_id": "r"}, item(), governance_candidate=candidate)
        self.assertIn("observation_signal candidates are diagnostic", text)
        self.assertIn("do not promote", text)
        self.assertNotIn("promote-decision when a candidate exists", text)

    def test_continuation_preserves_substantive_result_and_runtime_provenance(self):
        text = messages.build_continuation_message({"run_id": "r"}, item())
        self.assertIn("complete standalone final answer", text)
        self.assertIn("Preserve the substantive answer", text)
        self.assertIn("runtime/tool", text)
        self.assertIn("not new user authorization", text)

    def test_real_adapter_error_preserves_original_goal_and_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(Path(tmp) / "not-a-directory")
            Path(env["GHOST_ALICE_AUTOPILOT_RUN_DIR"]).write_text("occupied")
            result = subprocess.run([sys.executable, str(SKILL / "adapters/autopilot_mode.py")], input=json.dumps({"hook_event_name": "Stop"}), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        text = payload["reason"]
        self.assertIn("autopilot-mode adapter error", text)
        self.assertIn("original user goal", text)
        self.assertIn("complete standalone final answer", text)
        self.assertIn("Preserve the substantive answer", text)
        self.assertIn("runtime/tool", text)
        self.assertIn("not new user authorization", text)
        self.assert_recovery_proof_handoff(text)


class RecoveryFinalGrammarTest(unittest.TestCase):
    """Synthetic final surfaces exercise real validators, not native behavior.

    The historical business evidence is a fixture. The record readback is real;
    neither validator establishes truth or freshness from the evidence text.
    """

    def test_runtime_proof_needs_a_separate_final_block_with_retained_evidence(self):
        old_evidence = "readback at 2026-01-02T03:04:05Z; source: business-run/readback.log"
        prior_proof = VALID.replace("readback compared the exact bytes", old_evidence)
        completed = complete(prior_proof)[0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completion"]["evidence"], [prior_proof])
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "completion-record.json"
            record.write_text(json.dumps(completed), encoding="utf-8")
            self.assertEqual(json.loads(record.read_text(encoding="utf-8")), completed)
            runtime_evidence = f"recovery readback of {record} matched the saved completion record"
            proof = prior_proof.replace(
                "[completion-check]\n",
                "[completion-check]\n- verification-before-completion: done\n"
                "- skill-call: verification-before-completion (this turn)\n",
            ).replace(
                "- claim-evidence-map:",
                "  - record: Preserve the completion record [source: inferred]\n- claim-evidence-map:",
            ).replace(
                "- unverified:",
                "  - claim: The completion record matches the saved result\n"
                f"    criterion: record\n    evidence: {runtime_evidence}\n    verdict: pass\n- unverified:",
            )
            summary = "The requested change is complete. The business proof is retained from the original run.\n"
            trace = "[io-trace]\n- skills-loaded: verification-before-completion\n"
            validator = core_validator()
            self.assertIsNotNone(validator.validate_completion_text(summary + trace, require_completion_check=True))
            final = summary + proof + "\n" + trace
            self.assertIsNone(validator.validate_completion_text(final, require_completion_check=True))
            entries = validator.extract_claim_evidence_entries(validator.extract_control_block(final, "completion-check"))
            self.assertEqual(entries[0]["evidence"], old_evidence)
            self.assertEqual(entries[1]["evidence"], runtime_evidence)
            self.assertIsNotNone(validator.validate_completion_text(final.replace(trace, ""), require_completion_check=True))

    def test_unsupported_business_proof_remains_partial_despite_record_evidence(self):
        unsupported = VALID.replace("verdict: pass", "verdict: unverified").replace(
            "- unverified:\n  - none", "- unverified: business artifact changed after the recorded check",
        ).replace(
            "- claim-evidence-map:",
            "  - record: Retain the runtime record [source: inferred]\n- claim-evidence-map:",
        ).replace(
            "- unverified:",
            "  - claim: The runtime record contains the saved state\n"
            "    criterion: record\n    evidence: record readback\n    verdict: pass\n- unverified:",
        )
        with self.assertRaises(work_items.AutopilotStateError):
            complete(unsupported)
        self.assertIsNotNone(core_validator().validate_completion_evidence_map(unsupported))
        partial = (
            "The report artifact changed after its recorded check; its current correctness is unverified. "
            "The runtime record readback does not establish the report result. "
            "The affected business check remains open."
        )
        self.assertIsNone(core_validator().validate_completion_text(partial, require_completion_check=True))



if __name__ == "__main__":
    unittest.main()
