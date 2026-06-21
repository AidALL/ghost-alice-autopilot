from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "fresh_install_e2e.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("fresh_install_e2e", HARNESS)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_container_script_runs_default_install_before_platform_status_checks() -> None:
    harness = _load_harness()

    script = harness.container_script()
    default_install = 'bash "$CORE_REPO/install.sh" --addon-source "$ADDON_REPO"'

    assert default_install in script
    assert script.index(default_install) < script.index("--platform claude --status")
    assert script.index(default_install) < script.index("--platform codex --status")


def test_container_script_checks_hooks_and_installed_artifact_smoke_for_both_platforms() -> None:
    harness = _load_harness()

    script = harness.container_script()

    assert "/fresh-home/.claude/settings.json" in script
    assert "/fresh-home/.codex/hooks.json" in script
    assert "adapter_marker_count" in script
    assert "legacy_stop_inject_marker_count" in script
    assert "legacy_reset_count_marker_count" in script
    assert "run_installed_adapter_smoke(\"claude\"" in script
    assert "run_installed_adapter_smoke(\"codex\"" in script


def test_semantic_scenarios_are_intent_derived_without_raw_transcripts() -> None:
    harness = _load_harness()

    scenarios = harness.semantic_scenarios()

    assert len(scenarios) >= 4
    assert {scenario["source_platform"] for scenario in scenarios} >= {"claude", "codex"}
    assert {scenario["id"] for scenario in scenarios} >= {
        "observer-default-install-scope-gap",
        "plan-updates-during-testing",
        "verify-primary-artifact-before-claim",
        "execute-bounded-work-not-explain-only",
    }
    for scenario in scenarios:
        assert scenario["origin"] == "synthetic-from-session-intent-summary"
        assert scenario["source_session_id"]
        assert scenario["prompt"]
        assert scenario["expected_observations"]
        assert scenario["acceptance_criteria"]
        forbidden_keys = {"raw_prompt", "transcript", "message_log", "conversation"}
        assert not (forbidden_keys & set(scenario))


def test_container_script_writes_semantic_scenarios_and_auth_probe_summary() -> None:
    harness = _load_harness()

    script = harness.container_script()

    assert "/work/live-e2e-scenarios.json" in script
    assert "semantic_scenarios" in script
    assert "cli_auth_probe" in script
    assert "LIVE_CLI_MODE" in script
    assert "claude auth status" in script
    assert "codex login status" in script


def test_required_auth_probe_failure_reports_metadata_not_raw_output() -> None:
    harness = _load_harness()

    script = harness.container_script()

    assert 'sed -n \'1,80p\' "$out"' not in script
    assert 'auth_probe_failed %s rc=%s output_file=%s\\n' in script


def test_docker_run_args_mount_only_repositories_read_only() -> None:
    harness = _load_harness()

    args = harness.docker_run_args(
        image_tag="ghost-alice-fresh-install-e2e:test",
        core_repo=Path("/repo/ghost-alice"),
        addon_repo=Path("/repo/ghost-alice-autopilot"),
        run_script=Path("/tmp/run-inside-container.sh"),
    )
    joined = " ".join(args)

    assert "/repo/ghost-alice:/src/ghost-alice:ro" in joined
    assert "/repo/ghost-alice-autopilot:/src/ghost-alice-autopilot:ro" in joined
    assert "/.claude" not in joined
    assert "/.codex" not in joined
    assert "/.ghost-alice/secrets" not in joined
    assert "LIVE_CLI_MODE=probe" in joined
