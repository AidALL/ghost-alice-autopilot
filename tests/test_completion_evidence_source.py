"""Original-proof evidence derivation; only temporary authorities are mutated.

Dependencies: Python standard library, existing publication fixture, candidate Core.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock

import test_completion_publication as publication_tests


class CompletionEvidenceSourceTest(unittest.TestCase):
    setUp = publication_tests.CompletionPublicationTest.setUp
    prepare = publication_tests.CompletionPublicationTest.prepare
    proof = publication_tests.CompletionPublicationTest.proof
    read_state = publication_tests.CompletionPublicationTest.read_state
    change_intent = publication_tests.CompletionPublicationTest.change_intent
    stop = publication_tests.CompletionPublicationTest.stop

    def default_proof(self):
        self.proof()
        self.top_evidence = f"Original verification output at {self.verified_at}."
        self.completion += "- evidence: " + self.top_evidence + "\n"
        return self.completion

    def publish_default(self, receipt, **changes):
        args = dict(receipt_token=receipt["receipt_token"], completion_check=self.completion,
                    verified_at=self.verified_at, source=self.source)
        args.update(changes)
        try:
            return self.helper.publish_completion(**args)
        except TypeError as exc:
            self.fail(f"Default publication still requires redundant evidence input: {exc}")

    def cli_env(self):
        env = dict(os.environ, **self.source, PYTHONDONTWRITEBYTECODE="1")
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        return env

    def test_default_api_uses_top_level_value_without_selecting_claim_evidence(self):
        receipt = self.prepare(); original = self.default_proof()
        result = self.publish_default(receipt)
        self.assertEqual(result["evidence_source"], self.top_evidence)
        self.assertNotEqual(result["evidence_source"], self.evidence_source)
        self.assertEqual(result["completion_check_digest"], "sha256:" + hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(result["verified_at"], self.verified_at)
        self.assertEqual(self.publish_default(receipt), result)
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"unmet"})
        self.stop()
        task = publication_tests.adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(task["completion"]["evidence"], [original])
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_cli_omits_source_and_preserves_proof_time(self):
        receipt = self.prepare(); self.default_proof()
        argv = [sys.executable, "-B", str(publication_tests.SCRIPT), "publish", "--receipt-token",
                receipt["receipt_token"], "--verified-at", self.verified_at, "--completion-file", "-"]
        result = subprocess.run(argv, cwd=self.root, env=self.cli_env(), input=self.completion,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["evidence_source"], self.top_evidence)
        self.assertEqual(json.loads(result.stdout)["verified_at"], self.verified_at)

    def test_recovery_command_executes_without_locator_placeholder(self):
        self.prepare(); self.default_proof()
        message = self.stop()["systemMessage"]
        command = next(line for line in message.splitlines() if "autopilot_completion.py publish" in line)
        self.assertNotIn("--evidence-source", command)
        self.assertNotIn("ORIGINAL_LOCATOR", message)
        self.assertIn("top-level", message)
        argv = [self.verified_at if word == "ORIGINAL_ISO_TIME" else word for word in shlex.split(command)]
        result = subprocess.run(argv, cwd=self.root, env=self.cli_env(), input=self.completion,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["evidence_source"], self.top_evidence)

    def test_skill_default_omits_redundant_source_and_explains_limits(self):
        text = (Path(os.environ["GHOST_ALICE_CORE_ROOT"]) /
                "coding-convention/verification-before-completion/SKILL.md").read_text()
        default = next(line for line in text.splitlines() if "autopilot_completion.py publish" in line)
        self.assertNotIn("--evidence-source ORIGINAL_LOCATOR", default)
        for required in ("top-level", "placeholder", "unchanged", "nested", "exactly one"):
            self.assertIn(required, text)

    def test_multiline_section_preserves_exact_text_and_crlf(self):
        receipt = self.prepare(); original = self.default_proof()
        value = self.top_evidence + "\r\n  - tool-result:second-evidence; exit 0"
        self.completion = original.replace("\n", "\r\n").replace(self.top_evidence, value)
        self.assertEqual(self.publish_default(receipt)["evidence_source"], value)

    def supported_reference(self, value):
        receipt = self.prepare(); self.default_proof()
        self.completion = self.completion.replace(self.top_evidence, value)
        original = self.completion
        try:
            published = self.publish_default(receipt)
        except ValueError as exc:
            self.fail(f"Supported original evidence was rejected: {exc}")
        self.assertEqual(published["evidence_source"], value)
        self.assertEqual(published["completion_check_digest"], "sha256:" + hashlib.sha256(original.encode()).hexdigest())
        self.stop()
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_literal_completion_marker_in_evidence_is_not_a_second_block(self):
        self.supported_reference("Test output confirms `[completion-check]` parsing; exit 0.")

    def test_markdown_angle_link_is_not_a_placeholder(self):
        self.supported_reference("[Report](</tmp/My Report.txt>): exact original-byte readback; exit 0.")

    def test_placeholder_word_in_a_filename_is_not_a_placeholder(self):
        self.supported_reference("Readback of /tmp/TODO.md and /tmp/PLACEHOLDER.txt; exit 0.")

    def test_default_rejects_missing_empty_placeholder_duplicate_ambiguous_and_partial(self):
        receipt = self.prepare(); original = self.default_proof()
        header = "- evidence: " + self.top_evidence + "\n"
        cases = [original.replace(header, ""), original.replace(header, "- evidence:\n"),
                 original.replace(header, "- evidence: <ORIGINAL_LOCATOR>\n"),
                 original.replace(header, "- evidence: TODO\n"),
                 original.replace(header, "- evidence: none\n"),
                 original.replace(header, "- evidence: ...\n"),
                 original.replace(header, "- evidence:\n  - real result\n  - TBD\n"),
                 original.replace(header, "- evidence:\n  - real result\n  - \n"),
                 original + header, original + "[completion-check]\n",
                 original + "[io-trace]\n- evidence: unrelated\n",
                 "Prose before proof\n" + original,
                 original.replace("[completion-check]", "[completion-check partial]"),
                 original + "unindented partial evidence continuation\n",
                 original.replace("verdict: pass", "verdict: fail", 1),
                 original.replace("criterion: report", "criterion: unknown", 1),
                 original.replace("- unverified: none", "- unverified: pending"),
                 original.split("  - claim: Protected file is unchanged")[0] + "- unverified: none\n" + header]
        for proof in cases:
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.publish_default(receipt, completion_check=proof)
        with publication_tests.adapter.storage.transaction(self.run_dir, self.source) as store:
            self.assertEqual(store.read(self.helper.PUBLICATION_OBJECT)["status"], "prepared")
        self.assertFalse(publication_tests.adapter.storage.pending(self.run_dir / "consistency-decision.json"))

    def test_explicit_legacy_override_keeps_membership_and_empty_rejection(self):
        receipt = self.prepare(); self.proof()  # Legacy proof has nested evidence only.
        for value in ("", "a mismatched reference"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.publish_default(receipt, evidence_source=value)
        result = self.publish_default(receipt, evidence_source=self.evidence_source)
        self.assertEqual(result["evidence_source"], self.evidence_source)

    def test_default_keeps_stale_receipt_and_original_time_checks(self):
        receipt = self.prepare(); self.default_proof()
        for changes in ({"receipt_token": "sha256:" + "0" * 64},
                        {"verified_at": datetime.now(timezone.utc).isoformat()},
                        {"verified_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
                        {"source": dict(self.source, GHOST_ALICE_SESSION_ID="foreign")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.publish_default(receipt, **changes)
        self.change_intent({"constraints": ["Changed authorized scope"]})
        with self.assertRaises(ValueError): self.publish_default(receipt)

    def retained_proof(self, batch):
        fixture = RETAINED_PROOFS[batch]
        original = fixture["proof"]
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), fixture["sha256"])
        state = json.loads(self.state_path.read_text())
        state["acceptance_criteria"] = [{"id": name, "summary": "Retained " + name,
            "source": "user-explicit", "admitted": True, "status": "unmet"} for name in fixture["criteria"]]
        self.state_path.write_text(json.dumps(state))
        verified = datetime.fromisoformat(fixture["verified_at"])
        class OriginalPreparationClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return verified - timedelta(seconds=1)
        with mock.patch.object(self.helper, "datetime", OriginalPreparationClock):
            receipt = self.prepare()
        self.completion, self.verified_at = original, fixture["verified_at"]
        published = self.publish_default(receipt)
        self.assertEqual(published["evidence_source"], fixture["top_evidence"])
        self.assertEqual(published["completion_check_digest"], "sha256:" + fixture["sha256"])
        self.assertEqual(published["verified_at"], fixture["verified_at"])
        self.assertEqual(published["receipt_token"], receipt["receipt_token"])
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"unmet"})
        self.stop()
        task = publication_tests.adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(task["completion"]["evidence"], [original])
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_retained_batch2_exact_proof_default_publication(self):
        self.retained_proof(2)

    def test_retained_batch3_exact_proof_default_publication(self):
        self.retained_proof(3)


# Exact public stdin bytes, including final newline, from the retained failed calls.
# The matching successful historical calls have the same proof digests.
RETAINED_PROOFS = {2: {'proof': '[completion-check]\n- verification-before-completion: done\n- skill-call: verification-before-completion (this turn)\n- acceptance-criteria:\n  - backup: Backup matches the personal report before editing. [source: user-explicit]\n  - row-and-preservation: Preserve existing content and append the exact requested row. [source: user-explicit]\n  - target: Limit business-file writes to the personal report and backup. [source: user-explicit]\n- claim-evidence-map:\n  - claim: Backup matches the original.\n    criterion: backup\n    evidence: Verification output b1442d; original SHA-256 and byte comparison.\n    verdict: pass\n  - claim: Existing edits remain and only the requested row was appended.\n    criterion: row-and-preservation\n    evidence: Verification output b1442d; report equals backup plus the exact row.\n    verdict: pass\n  - claim: Only the designated personal files were written; the shared copy remains unchanged.\n    criterion: target\n    evidence: Write operation 074354; file guard output 1cfdca.\n    verdict: pass\n- unverified:\n  - none\n- evidence: Fresh verification at 2026-09-28T03:06:19.530685+00:00.\n', 'sha256': 'e55e850470cb20e53fd000a04ca89de94d4061a100f5d08b669133af3cf6f0c3', 'verified_at': '2026-09-28T03:06:19.530685+00:00', 'criteria': ['backup', 'row-and-preservation', 'target'], 'top_evidence': 'Fresh verification at 2026-09-28T03:06:19.530685+00:00.'}, 3: {'proof': '[completion-check]\n- verification-before-completion: done\n- skill-call: verification-before-completion (this turn)\n- acceptance-criteria:\n  - backup-original: Backup preserves the original personal report. [source: user-explicit]\n  - report-line-preservation: Preserve existing content and add the exact requested line. [source: user-explicit]\n  - task-scope: Limit task changes to the personal report and backup. [source: user-explicit]\n- claim-evidence-map:\n  - claim: Backup matches the original.\n    criterion: backup-original\n    evidence: Python byte comparison; exit 0.\n    verdict: pass\n  - claim: Original content remains with the requested line appended.\n    criterion: report-line-preservation\n    evidence: Exact byte comparison and displayed diff; exit 0.\n    verdict: pass\n  - claim: Shared report remains unchanged; writes targeted only the named personal files.\n    criterion: task-scope\n    evidence: Executed write paths and file_guard unchanged=true.\n    verdict: pass\n- unverified: none\n- evidence: Verification output at 2026-09-28T03:27:21.681111+00:00.\n', 'sha256': 'a4398972fc139900f191c3099c4d5ff62aac4c0a7300787b58fada03bf742a08', 'verified_at': '2026-09-28T03:27:21.681111+00:00', 'criteria': ['backup-original', 'report-line-preservation', 'task-scope'], 'top_evidence': 'Verification output at 2026-09-28T03:27:21.681111+00:00.'}}


if __name__ == "__main__":
    unittest.main()
