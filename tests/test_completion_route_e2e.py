"""Original-condition regressions through owning Core/AP source APIs.

GHOST_ALICE_CORE_ROOT must select the owning Core test checkout, as in AP's
existing completion tests. All identities, approvals, routes, and verification
sources below are owned test fixtures. Caller-selected routes and host_context
exercise consumers and transport; they do not establish model selection, native
visibility, native approval, installed behavior, or historical business truth.
The sanitized fixture is public and required. Its parser samples are data,
separate from the current fixture proof.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parent
AP_ROOT = PACKAGE.parent.resolve()
FIXTURE_DIGEST = "f095e532a3a255252d1ef6182cdd530b9043a29a8469154e2b5300e66591b861"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load owning module {path}")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def configured_pair():
    """Test source configuration is never a native approval or host authority."""
    configured = os.environ.get("GHOST_ALICE_CORE_ROOT")
    if not configured:
        raise RuntimeError("GHOST_ALICE_CORE_ROOT must configure the owning Core test checkout")
    core = Path(configured).resolve()
    if not core.is_dir() or not AP_ROOT.is_dir():
        raise RuntimeError("configured owning source root is unavailable")
    installed_skill = os.environ.get('GHOST_ALICE_TEST_AP_SKILL_ROOT')
    if installed_skill:
        skill = Path(installed_skill).resolve()
        if not (skill / 'adapters/autopilot_mode.py').is_file():
            raise RuntimeError('explicit installed AP test target is unavailable')
        return dict(core=core, ap=skill, skill=skill)
    return dict(core=core, ap=AP_ROOT, skill=AP_ROOT / 'addons/autopilot-mode/skill')


class CompletionRouteFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pair = configured_pair()
        cls.case_data_raw = (PACKAGE / "fixtures/completion_route_conditions.json").read_bytes()
        if digest(cls.case_data_raw) != FIXTURE_DIGEST:
            raise RuntimeError("sanitized public condition fixture changed")
        cls.case_data = json.loads(cls.case_data_raw)
        cls.historical_text = cls.case_data["historical_parser_analogue"]
        sys.dont_write_bytecode = True
        cls.skill = cls.pair['skill']
        sys.path.insert(0, str(cls.skill / "adapters"))
        sys.path.insert(0, str(cls.pair["core"] / "_shared"))
        cls.adapter = importlib.import_module("autopilot_state")
        cls.work_items = importlib.import_module("autopilot_work_items")
        cls.service = importlib.import_module("autopilot_completion_service")
        cls.storage = importlib.import_module("autopilot_storage")
        cls.mode = importlib.import_module("autopilot_mode")
        cls.pretool = importlib.import_module("autopilot_pretool")
        cls.validator = importlib.import_module("completion_check_validator")
        cls.helper = load_module("october_final_completion", cls.skill / "scripts/autopilot_completion.py")
        for component, module in (("core", cls.validator), ("ap", cls.adapter),
                ("ap", cls.work_items), ("ap", cls.service), ("ap", cls.storage),
                ("ap", cls.mode), ("ap", cls.pretool), ("ap", cls.helper)):
            cls.assert_loaded_source(component, module)

    @classmethod
    def assert_loaded_source(cls, component, module):
        origin = Path(module.__file__).resolve()
        selected_root = cls.pair[component].resolve()
        if not origin.is_relative_to(selected_root):
            raise RuntimeError(f"a stale/foreign module was loaded: {origin}")
        if not origin.is_file():
            raise RuntimeError(f"owning source is unavailable: {origin}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="october-test-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.business_count = 0
        self.stop_count = 0

    @staticmethod
    def criterion(identifier="design-quality-only", unit="october-test-current", summary="Preserve fixture data"):
        return dict(id=identifier, summary=summary, source="user-explicit", admitted=True,
                    status="unmet", work_unit_id=unit, contract_revision=1)

    def initialize(self, *, label, platform="codex", criteria=None, raw="Verify this isolated fixture"):
        self.platform = platform
        self.session = "october-test-" + label + "-" + platform
        self.intent_root = self.root / "intent"
        self.run_dir = self.root / ".autopilot/sessions" / platform / self.session
        self.source = dict(GHOST_ALICE_CORE_ROOT=str(self.pair["core"]),
            GHOST_ALICE_PLATFORM=platform, GHOST_ALICE_SESSION_ID=self.session,
            GHOST_ALICE_SESSION_INTENT_ROOT=str(self.intent_root),
            GHOST_ALICE_AUTOPILOT_CWD=str(self.root), GHOST_ALICE_COMPLETION_SCHEMA="v2", PWD=str(self.root),
            GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON='{"decision":"GO","source":"explicit fixture approval"}')
        skills = self.root / "managed-skills"
        skills.mkdir(exist_ok=True)
        if not (skills / "autopilot-mode").exists():
            (skills / "autopilot-mode").symlink_to(self.skill, target_is_directory=True)
        self.source["GHOST_ALICE_SKILLS_DIR"] = str(skills)
        self.core = self.work_items._load_core_ledger_module(
            self.intent_root / platform / self.session / "intent-state.json", self.source)
        if self.core is None:
            raise RuntimeError("accepted owning Core loader refused this isolated authority")
        self.assert_loaded_source("core", self.core)
        for name in ("record_turn", "read_session_state", "record_response_route", "read_response_route", "storage_transaction"):
            if not callable(getattr(self.core, name, None)):
                raise RuntimeError(f"configured Core is missing owning API: {name}")
        observation = self.core.build_input_observation(platform=platform, session_id=self.session, raw_user_input=raw)
        observation["input_event_id"] = "october-test-event-1"
        self.core.record_turn(root=self.intent_root, platform=platform, session_id=self.session,
            raw_user_input=raw, observation=observation,
            intent_delta={"current_goal": raw, "acceptance_criteria": criteria or [self.criterion()]})

    def state(self, *, transaction=None):
        return self.core.read_session_state(root=self.intent_root, platform=self.platform,
            session_id=self.session, transaction=transaction)

    def select_consumer_control(self, selected, *, mode="normal", primary="Current synthetic fixture", purpose="completion"):
        """Explicit test control, NOT an assertion that task-router/model selected this."""
        state = self.state()
        owned = [c for c in state["acceptance_criteria"] if c.get("work_unit_id") in selected]
        latest = {unit: max(c["contract_revision"] for c in owned if c["work_unit_id"] == unit) for unit in selected}
        units = [dict(work_unit_id=unit, contract_revision=latest[unit], kind="execution", admitted=True,
            owner=dict(platform=self.platform, session_id=self.session)) for unit in selected]
        references = [dict(work_unit_id=c["work_unit_id"], contract_revision=c["contract_revision"], criterion_id=c["id"])
            for c in owned if c["contract_revision"] == latest[c["work_unit_id"]] and c["admitted"]]
        surface = dict(intent_relation="continuation", primary_request=primary, causal_axis="n/a", response_mode=mode,
            response_order="imperative-result-first-after-necessary-work" if mode == "normal" else "resolved-intent-first",
            change_depth="localized", focus_layer="meso", verification_complexity="level-2",
            boundary_contract="required" if mode == "normal" else "n/a", forced_visibility="no",
            reason="Constructed consumer control; semantics require independent skill-pressure assessment")
        with self.core.storage_transaction(self.intent_root) as transaction:
            live = self.state(transaction=transaction)
            result = self.core.record_response_route(root=self.intent_root, platform=self.platform,
                session_id=self.session, expected_input_event_id=live["latest_input_event_id"],
                boundary_ref="october-test-boundary" if mode == "normal" else None,
                candidate=dict(schema_version="response-route.v1", response_purpose=purpose, routing_surface=surface, work_units=units,
                    selected_work_unit_ids=selected, selected_criteria=references), transaction=transaction)
        return result

    def prepare(self, *, source=None, v2=True):
        arguments = dict(intent_root=self.intent_root, platform=self.platform, session_id=self.session,
                         input_event_id=self.state()["latest_input_event_id"], source=source or self.source)
        if v2:
            arguments["schema_version"] = "v2"
        return self.helper.prepare_completion(**arguments)

    def isolated_business_proof(self, *, exact_body=False):
        body = self.case_data["current_fixture_body"] if exact_body else "isolated fixture payload"
        artifact = self.root / "business-result.txt"
        artifact.write_text(body, encoding="utf-8")
        self.business_count += 1
        self.assertEqual(artifact.read_text(encoding="utf-8"), body)
        verified = datetime.now(timezone.utc).isoformat()
        source = f"tool-result:october-test-{self.session}-business-1"
        proof = "\n".join(["[completion-check]", "- verification-before-completion: done",
            "- skill-call: verification-before-completion (this turn)", "- acceptance-criteria:",
            "  - design-quality-only: Preserve fixture data [source: user-explicit]", "- claim-evidence-map:",
            "  - claim: The isolated fixture readback preserves its input", "    criterion: design-quality-only",
            f"    evidence: {source}; fixture data readback", "    verdict: pass", "- unverified: none",
            "- evidence: " + source, ""])
        return proof, verified, source, body

    def review(self, response, verified=None, reuse_evidence=None):
        import completion_assessment
        artifact = self.root / "business-result.txt"
        if verified is None:
            artifact.write_text(response)
        candidate = completion_assessment.prepare_response_review(self.source,
            response_text=response, source_paths=[] if reuse_evidence else [str(artifact)],
            verified_at=verified, reuse_evidence=reuse_evidence,
            claim_statements=[response] if verified is None else None)
        ids = [row["source_id"] for row in candidate["sources"]]
        completion_assessment.record_response_review(self.source, candidate["candidate_reference"], {
            "claims": [dict(binding_id=row["binding_id"], verdict="supported", source_ids=ids,
                rationale="The explicit fixture observation supports this claim.") for row in candidate["bindings"]],
            "response": dict(binding_id=candidate["response_binding"]["binding_id"], verdict="supported",
                source_ids=ids, rationale="The complete fixture response reports the selected observations and scope.")})

    def publish(self, receipt, *, proof=None, verified=None, source=None, references=None, **changes):
        arguments = dict(receipt_token=receipt["receipt_token"], completion_check=proof,
            verified_at=verified, source=self.source, schema_version="v2")
        if source is not None:
            arguments["evidence_source"] = source
        if references is not None:
            arguments["evidence_references"] = references
        arguments.update(changes)
        if proof is not None:
            self.review(proof, verified)
        return self.helper.publish_completion(**arguments)

    def hook_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GHOST_ALICE_", "CODEX_", "CLAUDE_"))}
        env.update(self.source)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.platform == "codex":
            env["CODEX_THREAD_ID"] = self.session
        return env

    def executable_hook(self, script, payload, *, extra=()):
        # Exact owning no-argument AP adapter remains unmodified and registry/hash guarded.
        result = subprocess.run([sys.executable, "-B", str(script), *extra], cwd=self.root,
            env=self.hook_env(), input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def stop(self, *, body="", surface="visible_transcript", changed=False, subprocess_entry=True):
        self.stop_count += 1
        host = dict(hook_event_name="Stop", session_id=self.session, cwd=str(self.root),
                    last_assistant_message=body, host_context=dict(delivery_surface=surface), claims_changed=changed)
        if subprocess_entry:
            result = self.executable_hook(self.skill / "adapters/autopilot_mode.py", host)
        else:
            result = self.mode._format_payload_for_hook(self.adapter.adapter_payload_from_env(self.source, hook_input=host), host)
        return result

    def followup(self, raw="같은 작업의 상태를 이어서 알려 주세요."):
        previous = self.state()
        self.core.record_turn(root=self.intent_root, platform=self.platform, session_id=self.session,
            raw_user_input=raw, expected_input_event_id=previous["latest_input_event_id"])
        stale = self.core.read_response_route(root=self.intent_root, platform=self.platform, session_id=self.session)
        self.assertEqual(stale["status"], "stale")
        self.select_consumer_control(["october-test-current"])


class CompletionRouteE2ETest(CompletionRouteFixture):
    def test_core_review_and_native_publication_do_not_replay_business_work(self):
        self.initialize(label='visible-stop-owner')
        self.select_consumer_control(['october-test-current'])
        self.prepare()
        proof, verified, source, body = self.isolated_business_proof()
        guard = self.pair['core'] / '_shared/claude_stop_verification_hook.py'
        rejected = self.executable_hook(guard, dict(session_id=self.session,
            last_assistant_message=proof), extra=('--platform', 'codex'))
        self.assertEqual(rejected['decision'], 'block')
        self.assertIn('assessment-required', rejected['reason'])
        self.review(proof, verified)
        accepted = self.executable_hook(guard, dict(session_id=self.session,
            last_assistant_message=proof), extra=('--platform', 'codex'))
        self.assertEqual(accepted, {'continue': True})
        resolved = self.stop(body=proof)
        self.assertEqual(resolved['systemMessage'], '')
        self.assertEqual(resolved['completion_recording']['units'][0]['unit_status'], 'complete')
        self.assertNotIn('decision', resolved)
        self.assertEqual(self.business_count, 1)
        self.assertEqual((self.root / 'business-result.txt').read_text(), body)

    def test_case01_discussion_excludes_unrelated_backlog_and_readonly_delivery_twin(self):
        historical = self.criterion("old", "october-test-history", "Unrelated historical obligation")
        self.initialize(label="case01", criteria=[historical], raw="설계 선택지를 논의해 주세요. 아직 문서나 코드를 수정하지 마세요.")
        self.select_consumer_control([], mode="direct-response")
        before = self.state()
        capture = self.pretool.capture_before_tool(self.source, dict(hook_event_name="PreToolUse",
            session_id=self.session, cwd=str(self.root)), preflight=True)
        prepared = self.prepare()
        stopped = self.stop()
        self.assertEqual(capture["status"], "skipped")
        self.assertEqual(prepared["status"], "skipped")
        self.assertEqual(stopped["systemMessage"], "")
        self.assertEqual(before, self.state())
        self.assertFalse(self.run_dir.exists())
        # A current admitted read-only deliverable still requires its owning workflow.
        self.core.record_turn(root=self.intent_root, platform=self.platform, session_id=self.session,
            raw_user_input="읽기 전용 설계 평가 결과를 전달해 주세요.",
            expected_input_event_id=before["latest_input_event_id"],
            intent_delta={"acceptance_criteria": [self.criterion(summary="Deliver the admitted read-only evaluation")]})
        self.select_consumer_control(["october-test-current"], primary="Deliver admitted read-only evaluation")
        current = self.prepare()
        self.assertEqual(current["status"], "prepared")
        self.assertEqual(current["criterion_ids"], ["design-quality-only"])
        self.assertEqual(next(c for c in self.state()["acceptance_criteria"] if c["id"] == "old")["status"], "unmet")
        unsupported = self.executable_hook(self.pair["core"] / "_shared/claude_stop_verification_hook.py",
            dict(last_assistant_message="The read-only fixture review is complete.", stop_hook_active=False),
            extra=("--platform", self.platform))
        self.assertEqual(unsupported.get("decision"), "block")

    def test_case02_sanitized_current_body_publication_only_two_actual_adapter_stops(self):
        self.initialize(label="case02")
        self.select_consumer_control(["october-test-current"])
        receipt = self.prepare()
        proof, verified, source, body = self.isolated_business_proof(exact_body=True)
        first = self.stop(body=body)
        self.assertEqual(first["completion_recording"]["reason"], "completion-body-invalid")
        self.assertNotIn(body, first["systemMessage"])
        published = self.publish(receipt, proof=proof, verified=verified, source=source)
        complete_body = body + "\n" + proof
        self.review(complete_body, verified, reuse_evidence=published["evidence_references"][0])
        second = self.stop(body=complete_body)
        self.assertEqual(second["systemMessage"], "")
        self.assertEqual(second["completion_recording"]["units"][0]["original_evidence"], published["original_evidence"])
        self.assertEqual(self.stop_count, 2)
        self.assertEqual(self.business_count, 1)
        self.assertEqual((self.root / "business-result.txt").read_text(), body)

    def test_case02_delivery_final_only_unknown_and_changed_claim_twins(self):
        for surface, changed, expected, has_prior in (("final_only", False, "assembled_complete_payload", True),
                ("unknown", False, "resolve_host_delivery", True),
                ("visible_transcript", False, "assembled_complete_payload", False),
                ("visible_transcript", True, "corrected_complete_answer", True)):
            with self.subTest(surface=surface, changed=changed):
                # Each twin has its own authority and no previous diagnostic to park.
                twin = self.root / (surface + "-" + str(changed)); twin.mkdir()
                previous_root = self.root; self.root = twin
                try:
                    self.initialize(label="case02twin")
                    self.select_consumer_control(["october-test-current"])
                    self.prepare()
                    body = self.case_data["current_fixture_body"] if has_prior else ""
                    result = self.stop(body=body, surface=surface, changed=changed)
                    delivery = load_module("fixture_delivery", self.pair["core"] / "_shared/response_delivery.py")
                    decision = delivery.observe_recovery_delivery(dict(host_context=dict(delivery_surface=surface),
                        claims_changed=changed), platform=self.platform, final_text=body, repair_target="runtime_records")
                    self.assertEqual(decision["delivery_route"], expected)
                    if observed := result.get("recovery", {}).get("delivery_route"):
                        self.assertEqual(observed, expected)
                finally:
                    self.root = previous_root

    def test_case03_parser_data_and_current_rebinding_retain_source_time(self):
        # Exact pasted historical syntax is flattened and fails the canonical
        # contract. Preserve/reject it honestly; do not silently repair old data.
        with self.assertRaises(self.work_items.AutopilotStateError):
            self.work_items._parse_completion_evidence(self.historical_text)
        self.assertEqual(sum(line.startswith("claim:") for line in self.historical_text.splitlines()), 10)
        canonical = self.case_data["canonical_parser_control"]
        parsed = self.work_items._parse_completion_evidence(canonical)
        self.assertEqual(parsed["criterion_ids"], ["design-quality-only"])
        self.assertEqual(len(parsed["claims"]), self.case_data["historical_claim_map_count"])
        self.assertEqual(self.service._original_evidence_source(canonical), "historical-data:sanitized-source")
        # Historical parser data is never attached to the current fixture proof.
        self.initialize(label="case03")
        self.select_consumer_control(["october-test-current"])
        first = self.prepare()
        proof, verified, source, body = self.isolated_business_proof()
        publication = self.publish(first, proof=proof, verified=verified, source=source)
        self.followup()
        rebound = self.prepare()
        self.assertNotEqual(first["receipt_token"], rebound["receipt_token"])
        self.review(proof, verified, reuse_evidence=publication["evidence_references"][0])
        reused = self.publish(rebound, references=publication["evidence_references"])
        self.assertEqual(reused["verified_at"], verified)
        self.assertEqual(reused["evidence_sources"], [source])
        self.assertEqual(reused["original_evidence"], publication["original_evidence"])
        self.assertEqual(self.business_count, 1)
        with self.assertRaises(ValueError):
            self.publish(rebound, references=publication["evidence_references"], verified=datetime.now(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            self.publish(rebound, references=["sha256:" + "0" * 64])
        foreign_session = "october-test-foreign-owner"
        foreign_raw = "Verify this separately owned fixture"
        observation = self.core.build_input_observation(platform=self.platform,
            session_id=foreign_session, raw_user_input=foreign_raw)
        observation["input_event_id"] = "foreign-event-1"
        self.core.record_turn(root=self.intent_root, platform=self.platform,
            session_id=foreign_session, raw_user_input=foreign_raw, observation=observation,
            intent_delta={"current_goal": foreign_raw, "acceptance_criteria": [self.criterion()]})
        original_session = self.session
        self.session = foreign_session
        try:
            self.select_consumer_control(["october-test-current"])
        finally:
            self.session = original_session
        foreign = dict(self.source, GHOST_ALICE_SESSION_ID=foreign_session,
            GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir))
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=rebound["receipt_token"], completion_check=None,
                verified_at=None, evidence_references=publication["evidence_references"], source=foreign, schema_version="v2")
        stopped = self.stop(body=proof)
        self.assertEqual(stopped["systemMessage"], "")
        self.assertEqual(self.business_count, 1)

    def test_case04_real_recording_exception_keeps_business_count_one_and_parks_identical_retry(self):
        self.initialize(label="case04")
        self.select_consumer_control(["october-test-current"])
        receipt = self.prepare()
        proof, verified, source, body = self.isolated_business_proof()
        published = self.publish(receipt, proof=proof, verified=verified, source=source)
        with self.storage.transaction(self.run_dir, self.source) as store:
            original_record = copy.deepcopy(store.completion_read("evidence", published["evidence_references"][0]))
        with mock.patch.object(self.work_items, "materialize_met_criteria_v2", side_effect=OSError("october-test recording failure")) as writer:
            first = self.stop(body=proof, subprocess_entry=False)
            second = self.stop(body=proof, subprocess_entry=False)
            self.assertEqual(first["recovery"]["status"], "diagnostic")
            self.assertEqual(second["recovery"]["status"], "parked")
            self.assertNotIn("decision", second)
            self.assertEqual(writer.call_count, 1)
        with self.storage.transaction(self.run_dir, self.source) as store:
            retained_record = store.completion_read("evidence", published["evidence_references"][0])
        self.assertEqual(retained_record, original_record)
        self.assertEqual(self.business_count, 1)
        self.assertEqual(published["verified_at"], verified)
        self.assertEqual(published["evidence_sources"], [source])
        self.assertEqual({c["status"] for c in self.state()["acceptance_criteria"]}, {"unmet"})

    def test_case05_status_input_stales_binding_without_erasing_ongoing_work(self):
        self.initialize(label="case05", raw="Continue until this isolated review deliverable is complete")
        self.select_consumer_control(["october-test-current"])
        before = self.state()
        self.core.record_turn(root=self.intent_root, platform=self.platform, session_id=self.session,
            raw_user_input="지금 어디까지 진행됐나요?", expected_input_event_id=before["latest_input_event_id"])
        current = self.state()
        self.assertEqual(current["acceptance_criteria"], before["acceptance_criteria"])
        route = self.core.read_response_route(root=self.intent_root, platform=self.platform, session_id=self.session)
        self.assertEqual(route["status"], "stale")
        self.assertEqual(route["selected_work_unit_ids"], ["october-test-current"])
        # Intentionally no caller-preset route and no automatic semantic-pass verdict.

    def test_case06_exact_korean_reports_follow_owned_review_across_platforms_and_retry(self):
        incomplete = self.case_data["korean_incomplete"]
        positive = self.case_data["korean_adjacent_positive"]
        for platform in ("codex", "claude"):
            for retry in (False, True):
                for text, expected in ((incomplete, None), (positive, "block")):
                    with self.subTest(platform=platform, retry=retry, text=text):
                        previous = self.root
                        self.root = previous / (platform + str(retry) + str(expected)); self.root.mkdir()
                        try:
                            self.initialize(label="korean", platform=platform)
                            self.select_consumer_control(["october-test-current"],
                                purpose="explanation" if expected is None else "completion")
                            if expected is None:
                                self.review(text)
                            payload = self.executable_hook(self.pair["core"] / "_shared/claude_stop_verification_hook.py",
                                dict(session_id=self.session, last_assistant_message=text, stop_hook_active=retry),
                                extra=("--platform", platform))
                            self.assertEqual(payload.get("decision"), expected)
                            if expected is None:
                                self.assertEqual(payload, {"continue": True})
                        finally:
                            self.root = previous

    def test_case07_processing_permissionerror_is_not_derived_acquisition(self):
        self.initialize(label="case07processing")
        self.select_consumer_control(["october-test-current"])
        with mock.patch.object(self.storage.RunStore, "completion_put", side_effect=PermissionError("october-test processing")):
            with self.assertRaises(PermissionError):
                self.prepare()
        with mock.patch.object(self.core, "storage_transaction", side_effect=PermissionError("october-test Core transaction")):
            with mock.patch.object(self.work_items, "_load_core_ledger_module", return_value=self.core):
                with self.assertRaises(PermissionError):
                    self.prepare()

    def test_case07_derived_explicit_acquisition_provenance_and_no_fallback(self):
        self.initialize(label="case07acquisition")
        self.select_consumer_control(["october-test-current"])
        with mock.patch.object(self.storage, "_atomic", side_effect=PermissionError("owned acquisition fixture")):
            derived = self.prepare()
        self.assertEqual(derived["status"], "recording-unavailable")
        self.assertEqual(derived["reason"], "derived-target-unwritable")
        self.assertFalse(self.run_dir.exists())
        explicit = dict(self.source, GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir))
        with mock.patch.object(self.storage, "_atomic", side_effect=PermissionError("owned explicit fixture")):
            with self.assertRaises(PermissionError):
                self.prepare(source=explicit)
        self.assertFalse(self.run_dir.exists())
        receipt = self.prepare()
        self.assertEqual(receipt["target_reference"]["selection_origin"], "derived")
        asserted = dict(self.source, GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir), GHOST_ALICE_AUTOPILOT_DERIVED_TARGET="true")
        explicit_receipt = self.prepare(source=asserted)
        self.assertEqual(explicit_receipt["target_reference"]["selection_origin"], "explicit")

    def test_case05_stop_cancel_replacement_and_historical_only_controls(self):
        for label, raw in (("stop", "Stop this work"), ("cancel", "Cancel this work"),
                           ("replacement", "Replace this request with a bounded explanation"),
                           ("historical", "Discuss the unrelated historical item")):
            with self.subTest(label=label):
                twin = self.root / label; twin.mkdir(); previous = self.root; self.root = twin
                try:
                    self.initialize(label=label, criteria=[self.criterion("old", "october-test-history")], raw=raw)
                    self.select_consumer_control([], mode="direct-response")
                    before = self.state()
                    self.assertEqual(self.prepare()["status"], "skipped")
                    self.assertEqual(self.stop()["systemMessage"], "")
                    self.assertEqual(before, self.state())
                    self.assertFalse(self.run_dir.exists())
                finally:
                    self.root = previous

    def legacy_approval_shape(self, *, fault=None):
        current = copy.deepcopy(self.state()["acceptance_criteria"][0])
        alias = dict(id=current["id"], summary="Unowned historical alias", source="user-explicit", admitted=True, status="unmet")
        history = dict(alias, work_unit_id="october-test-history", contract_revision=1)
        self.core.record_turn(root=self.intent_root, platform=self.platform, session_id=self.session,
            expected_input_event_id=self.state()["latest_input_event_id"], intent_delta={"acceptance_criteria": [alias, history]})
        self.select_consumer_control(["october-test-current"])
        self.source.pop("GHOST_ALICE_COMPLETION_SCHEMA", None)
        self.assertTrue(self.adapter._bootstrap_from_session_intent_if_approved(self.run_dir, self.source, self.root))
        # AP's own storage API creates a synthetic malformed approval.
        # Core authority was created/read only through its APIs; no SQL or JSON seeds.
        with self.storage.transaction(self.run_dir, self.source) as store:
            run = store.run(); tasks = store.read("tasks.jsonl")
            binding = run["approval_evidence"]["session_intent"]
            binding["acceptance_criteria"] = [alias, history, current]
            if fault == "duplicate":
                binding["acceptance_criteria"].append(copy.deepcopy(current))
            elif fault == "malformed_revision":
                binding["acceptance_criteria"].append(dict(current, contract_revision="1"))
            run["approval_generation"] = self.adapter.SESSION_MATERIAL.approval_generation(run)
            store.put_run(run); store.write("tasks.jsonl", tasks)

    def test_case08_qualified_ambiguity_typed_without_native_permission_denial(self):
        for fault in ("duplicate", "malformed_revision"):
            with self.subTest(fault=fault):
                twin = self.root / fault; twin.mkdir(); previous_root = self.root; self.root = twin
                try:
                    self.initialize(label="case08")
                    self.select_consumer_control(["october-test-current"])
                    self.legacy_approval_shape(fault=fault)
                    before = self.state()
                    capture = self.pretool.capture_before_tool(self.source,
                        dict(hook_event_name="PreToolUse", session_id=self.session, cwd=str(self.root)), preflight=True)
                    self.assertEqual(capture["status"], "recording-unavailable")
                    self.assertEqual(self.prepare(v2=False)["status"], "recording-unavailable")
                    repair = self.executable_hook(self.skill / "adapters/autopilot_pretool.py",
                        dict(hook_event_name="PreToolUse", session_id=self.session, cwd=str(self.root), tool_name="Bash",
                             tool_input=dict(command="repair the current receipt-bound intake and route")))
                    self.assertTrue(repair["continue"])
                    self.assertNotEqual(repair.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                    first = self.stop()
                    second = self.stop()
                    # The native adapter's defensive fallback projects the typed
                    # acquisition/selection error into its per-unit envelope.
                    unit = first["completion_recording"]["units"][0]
                    self.assertEqual(first["completion_recording"]["status"], "v2-unit-state")
                    self.assertEqual(unit["status"], "recording-error")
                    self.assertEqual(unit["reason"], "approval-projection-invalid")
                    self.assertEqual(unit["error_type"], "CompletionRecordingError")
                    self.assertNotIn("permissionDecision", first.get("hookSpecificOutput", {}))
                    self.assertNotIn("permissionDecision", second.get("hookSpecificOutput", {}))
                    self.assertEqual(second.get("recovery", {}).get("status"), "parked")
                    self.assertNotIn("decision", second)
                    self.assertEqual(before, self.state())
                finally:
                    self.root = previous_root

    def test_case08_unowned_and_historical_aliases_do_not_import_obligations(self):
        self.initialize(label="case08aliases")
        self.select_consumer_control(["october-test-current"])
        self.legacy_approval_shape()
        before = self.state()
        capture = self.pretool.capture_before_tool(self.source,
            dict(hook_event_name="PreToolUse", session_id=self.session, cwd=str(self.root)), preflight=True)
        self.assertEqual(capture["status"], "prospective")
        self.assertEqual(before, self.state())
        self.assertEqual(self.prepare(v2=False)["reason"], "v1-approval-needs-reapproval")
        self.assertEqual([c["work_unit_id"] for c in self.core.read_response_route(root=self.intent_root,
            platform=self.platform, session_id=self.session)["selected_criteria"]], ["october-test-current"])


if __name__ == "__main__":
    unittest.main()
