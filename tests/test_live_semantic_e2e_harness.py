from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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


def test_parse_claude_stream_json_keeps_auth_failure_separate_from_semantic_result(tmp_path: Path) -> None:
    harness = _load_harness()
    stream = tmp_path / "claude.jsonl"
    stream.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}),
                json.dumps({"type": "system", "subtype": "hook_response", "hook_event": "UserPromptSubmit"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": True,
                        "api_error_status": 401,
                        "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = harness.parse_claude_stream_json(stream)

    assert summary["runtime"] == "claude"
    assert summary["inference_status"] == "auth-failed"
    assert summary["semantic_status"] == "not-run"
    assert summary["api_error_status"] == 401
    assert summary["hook_events"] == {"SessionStart": 1, "UserPromptSubmit": 1}
    assert summary["hook_status"] == "not-applicable"


def test_live_scenarios_wrap_source_prompt_as_evaluation_not_execution(monkeypatch) -> None:
    harness = _load_harness()

    class SourceModule:
        @staticmethod
        def semantic_scenarios():
            return [
                {
                    "id": "execute",
                    "prompt": "Execute the next safe implementation step.",
                }
            ]

    monkeypatch.setattr(harness, "_fresh_install_module", lambda: SourceModule)

    scenario = harness.live_scenarios(scenario_source="static")[0]

    assert "Do not execute" in scenario.prompt
    assert "Evaluate the scenario text" in scenario.prompt
    assert "Execute the next safe implementation step." in scenario.prompt


def test_intent_sourced_live_scenarios_build_prompt_from_conduct_feedback(tmp_path: Path) -> None:
    harness = _load_harness()
    state_dir = tmp_path / "session-intent" / "codex" / "session-1"
    state_dir.mkdir(parents=True)
    (state_dir / "intent-state.json").write_text(
        json.dumps(
            {
                "current_goal": "Upgrade autopilot behavior from accumulated intent feedback.",
                "user_intent_summary": "Use actual session-intent deltas for semantic tests.",
                "constraints": ["Do not replay raw transcripts."],
                "acceptance_criteria": [
                    {
                        "id": "reuse-intent",
                        "summary": "Build semantic prompts from compressed intent artifacts.",
                    }
                ],
                "conduct_feedback": [
                    {
                        "id": "use-real-intent",
                        "status": "open",
                        "summary": "Hand-written synthetic scenarios do not exercise the real accumulated intent.",
                        "corrective_rule": "Generate semantic tests from conduct_feedback and acceptance_criteria.",
                        "failure_pattern": "hard-coded scenario drift",
                    }
                ],
                "raw_prompt": "must not appear",
                "transcript": "must not appear",
            }
        ),
        encoding="utf-8",
    )

    scenarios = harness.intent_sourced_live_scenarios(intent_roots=[tmp_path / "session-intent"])

    by_id = {scenario.id: scenario for scenario in scenarios}
    assert set(by_id) == {
        "intent-codex-session-1-use-real-intent",
        "intent-codex-session-1-acceptance-criteria",
    }
    scenario = by_id["intent-codex-session-1-use-real-intent"]
    assert "Compressed intent source:" in scenario.prompt
    assert "Source platform: codex" in scenario.prompt
    assert "Do not use tools" in scenario.prompt
    assert "Do not browse" in scenario.prompt
    assert "Do not execute shell commands" in scenario.prompt
    assert "Current goal: Upgrade autopilot behavior from accumulated intent feedback." in scenario.prompt
    assert "Use actual session-intent deltas for semantic tests." in scenario.prompt
    assert "Hand-written synthetic scenarios do not exercise" in scenario.prompt
    assert "Generate semantic tests from conduct_feedback" in scenario.prompt
    assert "Build semantic prompts from compressed intent artifacts." in scenario.prompt
    assert "Respond in compact JSON with keys: verdict, mismatch_detected" in scenario.prompt
    assert "must not appear" not in scenario.prompt


def test_intent_sourced_live_scenarios_include_acceptance_even_with_feedback(tmp_path: Path) -> None:
    harness = _load_harness()
    state_dir = tmp_path / "session-intent" / "codex" / "session-criteria"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "intent-state.json"
    state_path.write_text(
        json.dumps(
            {
                "current_goal": "Verify a plan and repair implementation drift.",
                "conduct_feedback": [
                    {
                        "id": "under-tested",
                        "summary": "The agent ran too narrow a semantic loop.",
                        "corrective_rule": "Run the full available intent surface.",
                    }
                ],
                "acceptance_criteria": [
                    {
                        "id": "all-surfaces",
                        "summary": "Exercise conduct feedback and acceptance criteria as separate semantic probes.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenarios = harness.intent_sourced_live_scenarios(intent_roots=[tmp_path / "session-intent"], limit=None)
    by_id = {scenario.id: scenario for scenario in scenarios}

    assert set(by_id) == {
        "intent-codex-session-criteria-under-tested",
        "intent-codex-session-criteria-acceptance-criteria",
    }
    criteria = by_id["intent-codex-session-criteria-acceptance-criteria"]
    assert criteria.source_locator == f"{state_path}#acceptance_criteria"
    assert "Exercise conduct feedback and acceptance criteria" in criteria.prompt


def test_intent_sourced_live_scenarios_include_decisions_and_open_questions(tmp_path: Path) -> None:
    harness = _load_harness()
    state_dir = tmp_path / "session-intent" / "claude" / "session-delta"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "intent-state.json"
    state_path.write_text(
        json.dumps(
            {
                "current_goal": "Continue implementation only after resolving decision and blocker deltas.",
                "decisions": [
                    {
                        "id": "run-in-place",
                        "text": "Apply the approved change in place rather than handing back a file artifact.",
                    }
                ],
                "open_questions": [
                    {
                        "id": "missing-source",
                        "text": "Which primary source artifact proves this claim?",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenarios = harness.intent_sourced_live_scenarios(intent_roots=[tmp_path / "session-intent"], limit=None)

    assert [scenario.id for scenario in scenarios] == [
        "intent-claude-session-delta-decisions",
        "intent-claude-session-delta-open-questions",
    ]
    assert scenarios[0].source_locator == f"{state_path}#decisions"
    assert "Apply the approved change in place" in scenarios[0].prompt
    assert scenarios[1].source_locator == f"{state_path}#open_questions"
    assert "Which primary source artifact proves this claim?" in scenarios[1].prompt


def test_live_scenarios_prefers_intent_source_when_available(tmp_path: Path, monkeypatch) -> None:
    harness = _load_harness()
    state_dir = tmp_path / "session-intent" / "claude" / "session-2"
    state_dir.mkdir(parents=True)
    (state_dir / "intent-state.json").write_text(
        json.dumps(
            {
                "current_goal": "Keep primary-artifact verification honest.",
                "conduct_feedback": [
                    {
                        "id": "verify-primary",
                        "summary": "Do not inherit reviewer verdicts.",
                        "corrective_rule": "Read primary artifacts before endorsing a claim.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(harness, "default_intent_roots", lambda: [tmp_path / "session-intent"])

    scenarios = harness.live_scenarios()

    assert [scenario.id for scenario in scenarios] == ["intent-claude-session-2-verify-primary"]


def test_select_scenarios_finds_explicit_intent_id_outside_default_limit(tmp_path: Path, monkeypatch) -> None:
    harness = _load_harness()
    root = tmp_path / "session-intent"
    for index in range(14):
        state_dir = root / "codex" / f"session-{index:02d}"
        state_dir.mkdir(parents=True)
        (state_dir / "intent-state.json").write_text(
            json.dumps(
                {
                    "current_goal": f"Goal {index}",
                    "conduct_feedback": [
                        {
                            "id": f"feedback-{index:02d}",
                            "summary": f"Feedback {index}",
                            "corrective_rule": f"Rule {index}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(harness, "default_intent_roots", lambda: [root])

    selected = harness.select_scenarios(
        ["intent-codex-session-13-feedback-13"],
        scenario_source="intent",
    )

    assert [scenario.id for scenario in selected] == ["intent-codex-session-13-feedback-13"]


def test_parse_codex_outputs_extracts_hooks_and_scenario_verdict(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="codex-ok", prompt="Return compact JSON.")
    log.write_text(
        "\n".join(
            [
                "hook: SessionStart",
                "hook: UserPromptSubmit",
                "hook: Stop",
                "tokens used",
                "123",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    last_message.write_text(
        json.dumps(
            {
                "verdict": "insufficient",
                "mismatch_detected": True,
                "focus_layer": "meso",
                "next_action": "Run both platforms",
                "loop_guard": "Close only on both surfaces",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["runtime"] == "codex"
    assert summary["inference_status"] == "ok"
    assert summary["semantic_status"] == "parsed"
    assert summary["hook_status"] == "complete"
    assert summary["hook_events"] == {"SessionStart": 1, "UserPromptSubmit": 1, "Stop": 1}
    assert summary["scenario_result"]["mismatch_detected"] is True


def test_parse_codex_outputs_accepts_json_with_trailing_io_trace(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="codex-json-plus-trace", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "update_plan_before_continue",
                "mismatch_detected": True,
                "focus_layer": "macro",
                "next_action": "Update the live plan before continuation.",
                "loop_guard": "Do not continue without evidence-bound completion.",
            }
        )
        + "\n\n[io-trace]\n- files-read: [example]\n",
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert summary["missing_keys"] == []
    assert summary["scenario_result"]["focus_layer"] == "macro"


def test_parse_codex_outputs_without_scenario_is_schema_unchecked(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    log.write_text("hook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "insufficient",
                "mismatch_detected": True,
                "focus_layer": "meso",
                "next_action": "Run both platforms",
                "loop_guard": "Close only on both surfaces",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0)

    assert summary["semantic_status"] == "schema-unchecked"


def test_parse_codex_outputs_flags_missing_expected_semantic_keys(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(
        id="schema-drift",
        prompt="Return compact JSON.",
        expected_keys=("verdict", "mismatch_detected", "next_action"),
    )
    log.write_text("hook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "insufficient",
                "mismatch_detected": True,
                "next_verification": "This is close, but not the requested key.",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["semantic_status"] == "schema-mismatch"
    assert summary["missing_keys"] == ["next_action"]


def test_codex_auth_failure_is_not_reported_as_parse_failure(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="codex-auth", prompt="Return compact JSON.")
    log.write_text("Error: not logged in. Run codex login.\n", encoding="utf-8")

    summary = harness.parse_codex_outputs(log, last_message, returncode=1, scenario=scenario)

    assert summary["inference_status"] == "auth-failed"
    assert summary["semantic_status"] == "not-run"
    assert summary["missing_keys"] == []


def test_codex_semantic_values_are_validated(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="codex-invalid", prompt="Return compact JSON.")
    log.write_text("hook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "",
                "mismatch_detected": "yes",
                "focus_layer": "",
                "next_action": "Run both platforms",
                "loop_guard": "",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["semantic_status"] == "semantic-invalid"
    assert summary["invalid_fields"] == ["verdict", "focus_layer", "loop_guard"]


def test_codex_structured_next_action_and_loop_guard_are_valid_semantic_values(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="codex-structured", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "reopen_focus_and_continue",
                "mismatch_detected": True,
                "focus_layer": "macro",
                "next_action": {
                    "classification": "update_plan_then_continue_execution",
                    "ask_user_only_for": ["OpenAI_key", "Google_OAuth_consent"],
                },
                "loop_guard": {
                    "no_defer_question": True,
                    "retry_same_unit_requires_concrete_failed_evidence": True,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)
    summary["scenario_id"] = scenario.id
    signal = harness.observation_signal_from_summary(summary, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert summary["invalid_fields"] == []
    assert signal["classification"] == "semantic-observation"
    assert signal["next_action"]["classification"] == "update_plan_then_continue_execution"
    assert signal["loop_guard"]["no_defer_question"] is True


def test_codex_structured_mismatch_detected_value_is_valid_semantic_bool(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="structured-mismatch", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "reopen_focus_before_continue",
                "mismatch_detected": {
                    "value": True,
                    "verified_vs_unverified": [
                        {
                            "premise": "gate invocation evidence is missing",
                            "status": "verified_from_artifact",
                        }
                    ],
                },
                "focus_layer": "meta",
                "next_action": {
                    "type": "update_plan_then_verify",
                    "verification": ["build verified/unverified premise map"],
                },
                "loop_guard": "Do not continue automatically.",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)
    summary["scenario_id"] = scenario.id
    signal = harness.observation_signal_from_summary(summary, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert summary["invalid_fields"] == []
    assert signal["mismatch_detected"] is True


def test_codex_structured_mismatch_detected_value_accepts_string_bool(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="structured-string-mismatch", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "continue",
                "mismatch_detected": {"value": "false"},
                "focus_layer": "meso",
                "next_action": "continue without reopening focus",
                "loop_guard": "continue only while criteria remain satisfied",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)
    signal = harness.observation_signal_from_summary(summary, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert summary["invalid_fields"] == []
    assert signal["mismatch_detected"] is False


def test_observation_signal_preserves_semantic_verdict(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="verification-request", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "request_verification",
                "mismatch_detected": False,
                "focus_layer": "verification-boundary",
                "next_action": "request source-backed verification before side effects",
                "loop_guard": "stop before completion claims without evidence",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)
    signal = harness.observation_signal_from_summary(summary, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert signal["verdict"] == "request_verification"
    assert signal["mismatch_detected"] is False


def test_codex_string_mismatch_detected_values_are_semantic_signals(tmp_path: Path) -> None:
    harness = _load_harness()
    scenario = harness.LiveScenario(id="string-mismatch", prompt="Return compact JSON.")
    cases = [
        ("yes: stale open_questions conflict with recorded decision", True),
        ("stale_open_questions_conflict_with_resolved_decision", True),
        ("surface-only", True),
        ("no intent mismatch; verification gap", True),
        ("false: no mismatch", False),
        ("none: no mismatch", False),
        ("no", False),
    ]
    for index, (mismatch_value, expected) in enumerate(cases):
        log = tmp_path / f"codex-{index}.log"
        last_message = tmp_path / f"codex-{index}.txt"
        log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
        last_message.write_text(
            json.dumps(
                {
                    "verdict": "request_verification",
                    "mismatch_detected": mismatch_value,
                    "focus_layer": "meso",
                    "next_action": "request verification before side effects",
                    "loop_guard": "stop before completion claims without evidence",
                }
            ),
            encoding="utf-8",
        )

        summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)
        signal = harness.observation_signal_from_summary(summary, scenario=scenario)

        assert summary["semantic_status"] == "parsed"
        assert summary["invalid_fields"] == []
        assert signal["mismatch_detected"] is expected


def test_parse_claude_stream_json_flags_missing_expected_semantic_keys(tmp_path: Path) -> None:
    harness = _load_harness()
    stream = tmp_path / "claude.jsonl"
    scenario = harness.LiveScenario(
        id="claude-schema-drift",
        prompt="Return compact JSON.",
        expected_keys=("verdict", "mismatch_detected", "next_action"),
    )
    stream.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "verdict": "insufficient",
                                            "mismatch_detected": True,
                                            "next_verification": "Wrong key for the requested schema.",
                                        }
                                    ),
                                }
                            ]
                        },
                    }
                ),
                json.dumps({"type": "result", "subtype": "success", "is_error": False}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = harness.parse_claude_stream_json(stream, scenario=scenario)

    assert summary["runtime"] == "claude"
    assert summary["semantic_status"] == "schema-mismatch"
    assert summary["missing_keys"] == ["next_action"]


def test_claude_semantic_values_are_validated(tmp_path: Path) -> None:
    harness = _load_harness()
    stream = tmp_path / "claude.jsonl"
    scenario = harness.LiveScenario(id="claude-invalid", prompt="Return compact JSON.")
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "verdict": "",
                                            "mismatch_detected": "yes",
                                            "focus_layer": "",
                                            "next_action": "Run both platforms",
                                            "loop_guard": "",
                                        }
                                    ),
                                }
                            ]
                        },
                    }
                ),
                json.dumps({"type": "result", "subtype": "success", "is_error": False}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = harness.parse_claude_stream_json(stream, scenario=scenario)

    assert summary["semantic_status"] == "semantic-invalid"
    assert summary["invalid_fields"] == ["verdict", "focus_layer", "loop_guard"]


def test_run_command_to_file_records_timeout(tmp_path: Path, monkeypatch) -> None:
    harness = _load_harness()
    output = tmp_path / "command.log"

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(harness.subprocess, "run", timeout_run)

    returncode = harness.run_command_to_file(["codex", "exec", "prompt"], output, timeout_sec=0.01)

    assert returncode == 124
    assert "timed out after 0.01 seconds" in output.read_text(encoding="utf-8")


def test_run_command_to_file_closes_child_stdin(tmp_path: Path, monkeypatch) -> None:
    harness = _load_harness()
    output = tmp_path / "command.log"
    captured_kwargs = {}

    class Completed:
        returncode = 0

    def fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return Completed()

    monkeypatch.setattr(harness.subprocess, "run", fake_run)

    returncode = harness.run_command_to_file(["codex", "exec", "prompt"], output)

    assert returncode == 0
    assert captured_kwargs["stdin"] == subprocess.DEVNULL


def test_codex_timeout_is_not_reported_as_schema_mismatch(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(
        id="timeout",
        prompt="Return compact JSON.",
        expected_keys=("verdict", "mismatch_detected", "next_action"),
    )
    log.write_text("[live-semantic-e2e] command timed out\n", encoding="utf-8")

    summary = harness.parse_codex_outputs(log, last_message, returncode=124, scenario=scenario)

    assert summary["inference_status"] == "timeout"
    assert summary["semantic_status"] == "not-run"
    assert summary["missing_keys"] == []


def test_codex_timeout_after_tool_activity_is_classified_as_tool_loop(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="tool-loop", prompt="Return compact JSON.")
    log.write_text(
        "\n".join(
            [
                "hook: PreToolUse",
                "hook: PreToolUse",
                "hook: PostToolUse",
                "[live-semantic-e2e] command timed out after 120.0 seconds",
            ]
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=124, scenario=scenario)

    assert summary["inference_status"] == "timeout"
    assert summary["agent_activity"] == "tool-loop-timeout"


def test_observation_signal_from_tool_loop_timeout_summary_targets_reopen_signal(tmp_path: Path) -> None:
    harness = _load_harness()
    scenario = harness.LiveScenario(id="tool-loop", prompt="Return compact JSON.")
    output = tmp_path / "codex.log"

    signal = harness.observation_signal_from_summary(
        {
            "runtime": "codex",
            "scenario_id": "tool-loop",
            "inference_status": "timeout",
            "semantic_status": "not-run",
            "hook_status": "not-applicable",
            "missing_hooks": [],
            "agent_activity": "tool-loop-timeout",
            "returncode": 124,
            "output_file": str(output),
        },
        scenario=scenario,
    )

    assert signal["schema_version"] == "autopilot-observation-signal.v1"
    assert signal["source"] == "live_semantic_e2e"
    assert signal["runtime"] == "codex"
    assert signal["scenario_id"] == "tool-loop"
    assert signal["classification"] == "tool-loop-timeout"
    assert signal["focus_layer"] == "meso"
    assert signal["loop_guard"] == "tool-loop-timeout"
    assert signal["mismatch_detected"] is True
    assert signal["evidence"]["output_file"] == str(output)


def test_observation_signal_marks_auth_failure_as_readiness_blocker() -> None:
    harness = _load_harness()

    signal = harness.observation_signal_from_summary(
        {
            "runtime": "claude",
            "scenario_id": "auth",
            "inference_status": "auth-failed",
            "semantic_status": "not-run",
            "hook_status": "not-applicable",
            "missing_hooks": [],
            "api_error_status": 401,
        },
    )

    assert signal["classification"] == "readiness-blocker"
    assert signal["focus_layer"] is None
    assert signal["mismatch_detected"] is False


def test_codex_direct_json_answer_is_classified_as_direct_answer(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="direct", prompt="Return compact JSON.")
    log.write_text("hook: SessionStart\nhook: UserPromptSubmit\nhook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "insufficient",
                "mismatch_detected": True,
                "focus_layer": "meso",
                "next_action": "Run both platforms",
                "loop_guard": "Close only on both surfaces",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["semantic_status"] == "parsed"
    assert summary["hook_status"] == "complete"
    assert summary["agent_activity"] == "direct-answer"


def test_codex_parsed_json_without_required_hooks_is_hook_incomplete(tmp_path: Path) -> None:
    harness = _load_harness()
    log = tmp_path / "codex.log"
    last_message = tmp_path / "codex.txt"
    scenario = harness.LiveScenario(id="missing-hooks", prompt="Return compact JSON.")
    log.write_text("hook: Stop\n", encoding="utf-8")
    last_message.write_text(
        json.dumps(
            {
                "verdict": "insufficient",
                "mismatch_detected": True,
                "focus_layer": "meso",
                "next_action": "Run both platforms",
                "loop_guard": "Close only on both surfaces",
            }
        ),
        encoding="utf-8",
    )

    summary = harness.parse_codex_outputs(log, last_message, returncode=0, scenario=scenario)

    assert summary["semantic_status"] == "hook-incomplete"
    assert summary["hook_status"] == "missing-required"
    assert summary["missing_hooks"] == ["SessionStart", "UserPromptSubmit"]


def test_claude_parsed_json_without_required_hooks_is_hook_incomplete(tmp_path: Path) -> None:
    harness = _load_harness()
    stream = tmp_path / "claude.jsonl"
    scenario = harness.LiveScenario(id="claude-missing-hooks", prompt="Return compact JSON.")
    stream.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "verdict": "insufficient",
                                            "mismatch_detected": True,
                                            "focus_layer": "meso",
                                            "next_action": "Run both platforms",
                                            "loop_guard": "Close only on both surfaces",
                                        }
                                    ),
                                }
                            ]
                        },
                    }
                ),
                json.dumps({"type": "result", "subtype": "success", "is_error": False}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = harness.parse_claude_stream_json(stream, scenario=scenario)

    assert summary["semantic_status"] == "hook-incomplete"
    assert summary["hook_status"] == "missing-required"
    assert summary["missing_hooks"] == ["UserPromptSubmit", "Stop"]


def test_claude_timeout_is_not_reported_as_schema_mismatch(tmp_path: Path) -> None:
    harness = _load_harness()
    stream = tmp_path / "claude.jsonl"
    scenario = harness.LiveScenario(
        id="timeout",
        prompt="Return compact JSON.",
        expected_keys=("verdict", "mismatch_detected", "next_action"),
    )
    stream.write_text("[live-semantic-e2e] command timed out\n", encoding="utf-8")

    summary = harness._mark_timeout(harness.parse_claude_stream_json(stream, scenario=scenario), 124)

    assert summary["inference_status"] == "timeout"
    assert summary["semantic_status"] == "not-run"
    assert summary["missing_keys"] == []
    assert summary["hook_status"] == "not-applicable"


def test_live_execution_timeout_is_opt_in_not_default_wall_clock_budget() -> None:
    harness = _load_harness()

    args = harness.parse_args(["--execute"])

    assert args.timeout_sec is None


def test_cli_accepts_semantic_scenario_source_selection() -> None:
    harness = _load_harness()

    args = harness.parse_args(["--scenario-source", "intent", "--execute"])

    assert args.scenario_source == "intent"


def test_execute_jsonl_emits_one_result_per_scenario(monkeypatch, capsys, tmp_path: Path) -> None:
    harness = _load_harness()
    scenarios = [
        harness.LiveScenario(id="one", prompt="Return compact JSON."),
        harness.LiveScenario(id="two", prompt="Return compact JSON."),
    ]

    monkeypatch.setattr(harness, "select_scenarios", lambda ids, *, scenario_source="auto": scenarios)

    def fake_run_scenario(runtime, scenario, out_dir, *, timeout_sec=None):
        return {
            "runtime": runtime,
            "scenario_id": scenario.id,
            "inference_status": "ok",
            "semantic_status": "parsed",
            "hook_status": "complete",
        }

    monkeypatch.setattr(harness, "run_scenario", fake_run_scenario)

    rc = harness.main(
        [
            "--runtime",
            "codex",
            "--execute",
            "--jsonl",
            "--out-dir",
            str(tmp_path),
        ]
    )

    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {
            "live_semantic_e2e_result": {
                "runtime": "codex",
                "scenario_id": "one",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
            }
        },
        {
            "live_semantic_e2e_result": {
                "runtime": "codex",
                "scenario_id": "two",
                "inference_status": "ok",
                "semantic_status": "parsed",
                "hook_status": "complete",
            }
        },
    ]


def test_command_builders_do_not_embed_secret_values(tmp_path: Path) -> None:
    harness = _load_harness()
    scenario = harness.LiveScenario(
        id="probe",
        prompt="Return compact JSON.",
        expected_keys=("verdict", "mismatch_detected"),
    )

    claude = harness.build_claude_command(scenario, tmp_path / "claude.jsonl", max_budget_usd=0.05)
    codex = harness.build_codex_command(scenario, tmp_path / "codex.log", tmp_path / "codex.txt")
    joined = " ".join(claude + codex)

    assert "ANTHROPIC_API_KEY" not in joined
    assert "OPENAI_API_KEY" not in joined
    assert "--max-budget-usd" in claude
    assert "--output-last-message" in codex
    assert "--sandbox" in codex
    assert "read-only" in codex
