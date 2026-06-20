#!/usr/bin/env python3
"""Write an autopilot consistency decision from a completion signal.

This script belongs to the autopilot addon, not Ghost-ALICE core. It consumes a
final response or an explicit operator decision and writes the
`.autopilot/consistency-decision.json` file that the thin privileged adapter
consumes on the next Stop event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True

EXPLICIT_DECISIONS = frozenset(
    {"retry_same_unit", "reopen_micro", "reopen_meso", "reopen_macro", "ask_user_meta", "stop"}
)

BLOCK_HEADER_RE = re.compile(r"^\[([a-z0-9-]+)\]", re.I)
CLAIM_RE = re.compile(r"^\s*-\s*claim\s*:\s*(.+?)\s*$", re.I)
ENTRY_FIELD_RE = re.compile(r"^\s*(criterion|evidence|verdict)\s*:\s*(.+?)\s*$", re.I)
SKILL_CALL_RE = re.compile(r"-\s*skill-call:\s*([^\n]+)", re.I)
SKILLS_LOADED_RE = re.compile(r"-\s*skills-loaded:\s*\[([^\]]*)\]", re.I)


class DecisionError(ValueError):
    """Raised when a decision cannot be produced safely."""


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _control_block(text: str, name: str) -> str:
    wanted = name.lower()
    kept: list[str] = []
    in_block = False
    for line in text.splitlines():
        header = BLOCK_HEADER_RE.match(line.strip())
        if header:
            if in_block:
                break
            if header.group(1).lower() == wanted:
                in_block = True
            continue
        if in_block:
            kept.append(line)
    return "\n".join(kept).strip()


def _top_level_section(block: str, field_name: str) -> str:
    pattern = re.compile(r"^-\s*" + re.escape(field_name) + r"\s*:", re.I)
    lines = block.splitlines()
    start = -1
    for index, line in enumerate(lines):
        if pattern.search(line):
            start = index
            break
    if start < 0:
        return ""
    kept: list[str] = []
    for line in lines[start + 1 :]:
        if re.search(r"^-\s*[A-Za-z0-9_-]+\s*:", line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _claim_evidence_entries(completion_check: str) -> list[dict[str, str]]:
    section = _top_level_section(completion_check, "claim-evidence-map")
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in section.splitlines():
        claim = CLAIM_RE.match(line)
        if claim:
            current = {"claim": claim.group(1).strip()}
            entries.append(current)
            continue
        if current is None:
            continue
        field = ENTRY_FIELD_RE.match(line)
        if field:
            current[field.group(1).lower()] = field.group(2).strip()
    return entries


def _unverified_is_none(completion_check: str) -> bool:
    section = _top_level_section(completion_check, "unverified")
    lines = [line.strip().lower() for line in section.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("- none") for line in lines)


def _validation_issue(final_response: str, completion_check: str, entries: list[dict[str, str]]) -> str | None:
    if not completion_check:
        return "Completion signal requires a [completion-check] block."
    if "- verification-before-completion: done" not in completion_check:
        return "[completion-check] must include verification-before-completion: done."
    skill_call = SKILL_CALL_RE.search(completion_check)
    if not skill_call or "verification-before-completion" not in skill_call.group(1):
        return "[completion-check] skill-call must include verification-before-completion."
    io_trace = _control_block(final_response, "io-trace")
    skills_loaded = SKILLS_LOADED_RE.search(io_trace)
    if not skills_loaded or "verification-before-completion" not in skills_loaded.group(1):
        return "[io-trace] skills-loaded must include verification-before-completion."
    if not entries:
        return "[completion-check] must include non-empty claim-evidence-map."
    if not _unverified_is_none(completion_check):
        return "[completion-check] unverified section must be none."
    for entry in entries:
        if not entry.get("evidence"):
            return "Every claim-evidence-map entry must include evidence."
        verdict = entry.get("verdict", "").lower()
        if verdict not in {"pass", "fail"}:
            return "Every claim-evidence-map entry must include verdict: pass | fail."
    return None


def _decision_from_completion(work_item_id: str, final_response: str) -> dict:
    digest = _sha256_text(final_response)
    completion_check = _control_block(final_response, "completion-check")
    entries = _claim_evidence_entries(completion_check)
    issue = _validation_issue(final_response, completion_check, entries)
    if issue:
        return {
            "decision_id": f"decision-{uuid4().hex}",
            "work_item_id": work_item_id,
            "decision": "retry_same_unit",
            "completion_check_digest": digest,
            "evidence": [issue],
            "source": "autopilot-consistency-decision",
            "created_at": int(time.time()),
        }

    evidence = [entry["evidence"] for entry in entries if entry.get("evidence")]
    verdicts = [entry.get("verdict", "").lower() for entry in entries]
    all_pass = all(verdict == "pass" for verdict in verdicts)
    payload = {
        "decision_id": f"decision-{uuid4().hex}",
        "work_item_id": work_item_id,
        "decision": "continue_next" if all_pass else "retry_same_unit",
        "completion_check_digest": digest,
        "evidence": evidence,
        "source": "autopilot-consistency-decision",
        "created_at": int(time.time()),
    }
    if all_pass:
        payload["verdict"] = "pass"
    return payload


def _explicit_decision(work_item_id: str, decision: str, evidence: list[str]) -> dict:
    if decision not in EXPLICIT_DECISIONS:
        raise DecisionError(f"unsupported explicit decision: {decision}")
    cleaned = [item.strip() for item in evidence if item.strip()]
    if not cleaned:
        raise DecisionError("--evidence is required with --decision")
    return {
        "decision_id": f"decision-{uuid4().hex}",
        "work_item_id": work_item_id,
        "decision": decision,
        "evidence": cleaned,
        "source": "autopilot-consistency-decision",
        "created_at": int(time.time()),
    }


def _read_final_response(path: Path | None) -> str:
    try:
        return path.read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        raise DecisionError(f"could not read final response: {exc}") from exc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--work-item-id", required=True)
    parser.add_argument("--final-response", type=Path)
    parser.add_argument("--decision", choices=sorted(EXPLICIT_DECISIONS))
    parser.add_argument("--evidence", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.decision:
            decision = _explicit_decision(args.work_item_id, args.decision, args.evidence)
        else:
            decision = _decision_from_completion(args.work_item_id, _read_final_response(args.final_response))
        _write_json_atomic(args.run_dir.expanduser() / "consistency-decision.json", decision)
    except DecisionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 64
    except OSError as exc:
        sys.stderr.write(f"could not write decision: {exc}\n")
        return 64

    sys.stdout.write(json.dumps({"ok": True, "decision": decision["decision"], "work_item_id": args.work_item_id}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
