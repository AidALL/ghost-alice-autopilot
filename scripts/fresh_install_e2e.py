#!/usr/bin/env python3
"""Run Ghost-ALICE autopilot fresh-install checks inside Docker.

The harness mounts only the Ghost-ALICE core repo and this addon repo as
read-only inputs. It does not mount host Claude/Codex homes, auth files, or
secrets. The container uses a fresh HOME and verifies install parity for both
Claude and Codex surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


DEFAULT_IMAGE_TAG = "ghost-alice-fresh-install-e2e:local"
CONTAINER_SCRIPT_PATH = "/opt/ghost-alice-fresh-install-e2e/run-inside-container.sh"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_core_repo() -> Path:
    return repo_root().parent / "ghost-alice"


def dockerfile() -> str:
    return """\
FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
  && apt-get install -y --no-install-recommends \\
    bash \\
    ca-certificates \\
    git \\
    jq \\
    python3 \\
    rsync \\
  && rm -rf /var/lib/apt/lists/*

WORKDIR /work
"""


def semantic_scenarios() -> list[dict[str, Any]]:
    """Return synthetic E2E prompts derived from session-intent summaries.

    These are intentionally not raw prompts or transcripts. They preserve the
    behavioral pressure from prior Claude/Codex intent records while avoiding
    conversation-log replay.
    """

    return [
        {
            "id": "observer-default-install-scope-gap",
            "origin": "synthetic-from-session-intent-summary",
            "source_platform": "codex",
            "source_session_id": "019ee6b9-7a62-7a92-8d13-1b1a27e6fb9d",
            "conduct_feedback_ids": ["observer-default-install-scope-gap"],
            "prompt": (
                "Evaluate whether a fresh install test that passes only a platform-specific "
                "command is enough for an addon that must support both Claude and Codex. "
                "If the test narrows the default install path by accident, call out the gap "
                "and propose the next verification step."
            ),
            "expected_observations": [
                "Detect platform narrowing instead of treating a single-platform pass as default-install proof.",
                "Keep Claude and Codex install/status surfaces separate in the evidence map.",
            ],
            "acceptance_criteria": [
                "Names both Claude and Codex surfaces.",
                "Rejects platform-specific evidence as proof of default install parity.",
                "Proposes a bounded follow-up verification rather than an open-ended loop.",
            ],
        },
        {
            "id": "plan-updates-during-testing",
            "origin": "synthetic-from-session-intent-summary",
            "source_platform": "codex",
            "source_session_id": "autopilot-behavior-upgrade-plan",
            "conduct_feedback_ids": ["plan-updates-during-testing"],
            "prompt": (
                "You are executing an implementation plan and a test reveals that the original "
                "task order is wrong. Decide whether the plan needs to be updated, whether focus "
                "should move between micro, meso, macro, or meta, and what evidence should trigger "
                "rework instead of continuing blindly."
            ),
            "expected_observations": [
                "Treat the plan as a live artifact when evidence changes scope, ordering, or logic.",
                "Move focus according to mismatch location instead of only expanding scope.",
            ],
            "acceptance_criteria": [
                "States the mismatch that changes the next work decision.",
                "Selects a focus layer and explains why it changes or stays fixed.",
                "Defines a stop/rework condition that avoids infinite loops.",
            ],
        },
        {
            "id": "verify-primary-artifact-before-claim",
            "origin": "synthetic-from-session-intent-summary",
            "source_platform": "claude",
            "source_session_id": "1e6a453f-1e6c-4047-9c96-a918a20df834",
            "conduct_feedback_ids": [
                "verify-source-must-be-primary-artifact",
                "verification-workflow-must-be-truly-read-only",
                "verify-claim-scope-before-asserting-property",
            ],
            "prompt": (
                "A reviewer claims the adapter behavior is correct. Verify the claim without "
                "trusting the reviewer verdict. Use the primary artifact, keep the check read-only, "
                "and avoid claiming more than the inspected source proves."
            ),
            "expected_observations": [
                "Use a primary file or command output as evidence instead of inherited reviewer judgment.",
                "Separate source-backed findings from unverified assumptions.",
            ],
            "acceptance_criteria": [
                "Maps each claim to primary evidence.",
                "Keeps read-only verification separate from recovery or mutation.",
                "Scopes the verdict to what the evidence directly proves.",
            ],
        },
        {
            "id": "execute-bounded-work-not-explain-only",
            "origin": "synthetic-from-session-intent-summary",
            "source_platform": "claude",
            "source_session_id": "3bec3c02-9e01-4470-a1b6-2247b1ee2f92",
            "conduct_feedback_ids": [
                "defer-instead-of-execute",
                "plan-loop-not-implement",
                "infra-as-stop-excuse",
            ],
            "prompt": (
                "The user has approved a bounded implementation step. Do not stop at explaining "
                "what should happen. Execute the next safe step, report discovered blockers only "
                "when they are real, and avoid turning infrastructure uncertainty into a stop excuse."
            ),
            "expected_observations": [
                "Takes a bounded safe action instead of returning only a proposal.",
                "Distinguishes a real blocker from a need for more explanation.",
            ],
            "acceptance_criteria": [
                "Performs or identifies the next executable step.",
                "Avoids cycling through plan-only responses after implementation approval.",
                "Reports blockers with concrete evidence and a recovery path.",
            ],
        },
    ]


def container_script() -> str:
    script = r'''#!/usr/bin/env bash
set -Eeuo pipefail

export HOME=/fresh-home
export CLAUDE_CONFIG_DIR=/fresh-home/.claude
export CODEX_HOME=/fresh-home/.codex
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
AGENT_CLI_MODE="${AGENT_CLI_MODE:-probe}"
LIVE_CLI_MODE="${LIVE_CLI_MODE:-probe}"

mkdir -p "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" /work
cp -a /src/ghost-alice /work/ghost-alice
cp -a /src/ghost-alice-autopilot /work/ghost-alice-autopilot

CORE_REPO=/work/ghost-alice
ADDON_REPO=/work/ghost-alice-autopilot

emit_section() {
  printf '\n[fresh-e2e] %s\n' "$1"
}

emit_section "semantic-scenarios"
cat > /work/live-e2e-scenarios.json <<'JSON'
__SEMANTIC_SCENARIOS_JSON__
JSON
python3 - <<'PY'
from __future__ import annotations

import json
from pathlib import Path

path = Path("/work/live-e2e-scenarios.json")
scenarios = json.loads(path.read_text(encoding="utf-8"))
forbidden = {"raw_prompt", "transcript", "message_log", "conversation"}
bad = [
    scenario.get("id", "<missing-id>")
    for scenario in scenarios
    if forbidden.intersection(scenario)
]
if bad:
    raise SystemExit(f"[fresh-e2e] FAIL: semantic_scenarios contain raw fields: {bad}")
print(
    json.dumps(
        {
            "semantic_scenarios": {
                "path": str(path),
                "count": len(scenarios),
                "ids": [scenario["id"] for scenario in scenarios],
            }
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
)
PY

emit_section "runtime"
python3 --version
node --version
npm --version

if [ "$AGENT_CLI_MODE" != "skip" ]; then
  emit_section "agent-cli-install"
  set +e
  npm install -g @anthropic-ai/claude-code @openai/codex
  cli_install_rc=$?
  set -e
  printf '[fresh-e2e] agent-cli-install-rc=%s\n' "$cli_install_rc"
  if [ "$AGENT_CLI_MODE" = "required" ] && [ "$cli_install_rc" -ne 0 ]; then
    exit "$cli_install_rc"
  fi
  for cli in claude codex; do
    if command -v "$cli" >/dev/null 2>&1; then
      printf '[fresh-e2e] %s-path=%s\n' "$cli" "$(command -v "$cli")"
      "$cli" --version || true
    else
      printf '[fresh-e2e] %s-path=missing\n' "$cli"
      if [ "$AGENT_CLI_MODE" = "required" ]; then
        exit 127
      fi
    fi
  done
fi

run_auth_probe() {
  local label="$1"
  shift
  local out="/work/${label}-auth-status.txt"
  local rc_file="/work/${label}-auth-status.rc"
  printf '[fresh-e2e] cli_auth_probe %s command=%s\n' "$label" "$*"
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'missing executable: %s\n' "$1" > "$out"
    printf '127\n' > "$rc_file"
    printf '[fresh-e2e] cli_auth_probe %s rc=127\n' "$label"
    if [ "$LIVE_CLI_MODE" = "required" ]; then
      exit 127
    fi
    return 0
  fi
  set +e
  "$@" > "$out" 2>&1
  local rc=$?
  set -e
  printf '%s\n' "$rc" > "$rc_file"
  printf '[fresh-e2e] cli_auth_probe %s rc=%s\n' "$label" "$rc"
  if [ "$LIVE_CLI_MODE" = "required" ] && [ "$rc" -ne 0 ]; then
    printf '[fresh-e2e] auth_probe_failed %s rc=%s output_file=%s\n' "$label" "$rc" "$out"
    exit "$rc"
  fi
}

if [ "$LIVE_CLI_MODE" != "skip" ]; then
  emit_section "cli-auth-probe"
  run_auth_probe claude claude auth status
  run_auth_probe codex codex login status
  python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

summary = {
    "mode": os.environ.get("LIVE_CLI_MODE", "probe"),
    "commands": {},
}
for label in ("claude", "codex"):
    rc_path = Path(f"/work/{label}-auth-status.rc")
    out_path = Path(f"/work/{label}-auth-status.txt")
    summary["commands"][label] = {
        "rc": int(rc_path.read_text(encoding="utf-8").strip()),
        "output_file": str(out_path),
    }
Path("/work/cli-auth-probe.json").write_text(
    json.dumps({"cli_auth_probe": summary}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps({"cli_auth_probe": summary}, ensure_ascii=False, indent=2, sort_keys=True))
PY
fi

emit_section "default-install"
bash "$CORE_REPO/install.sh" --addon-source "$ADDON_REPO"

emit_section "status-claude"
bash "$CORE_REPO/install.sh" --platform claude --status --addon-source "$ADDON_REPO" | tee /work/status-claude.txt

emit_section "status-codex"
bash "$CORE_REPO/install.sh" --platform codex --status --addon-source "$ADDON_REPO" | tee /work/status-codex.txt

emit_section "artifact-and-hook-checks"
python3 - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


VALID_DIGEST = "sha256:" + ("a" * 64)


def fail(message: str) -> None:
    raise SystemExit(f"[fresh-e2e] FAIL: {message}")


def read_text(path: Path) -> str:
    if not path.is_file():
        fail(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def require_status(platform: str, path: Path) -> None:
    text = read_text(path)
    required = [
        "addon-registry: ok",
        "autopilot-mode/autopilot-mode (skill): content_hash-match",
        "autopilot-mode/autopilot-mode (adapter): content_hash-match",
        "overall: ok",
    ]
    missing = [item for item in required if item not in text]
    if missing:
        fail(f"{platform} status missing {missing}")


def semantic_scenario_summary(path: Path) -> dict[str, Any]:
    data = json.loads(read_text(path))
    required_ids = {
        "observer-default-install-scope-gap",
        "plan-updates-during-testing",
        "verify-primary-artifact-before-claim",
        "execute-bounded-work-not-explain-only",
    }
    ids = {item.get("id") for item in data}
    missing = sorted(required_ids - ids)
    if missing:
        fail(f"semantic scenario ids missing: {missing}")
    forbidden = {"raw_prompt", "transcript", "message_log", "conversation"}
    raw_field_hits = [
        item.get("id", "<missing-id>")
        for item in data
        if forbidden.intersection(item)
    ]
    if raw_field_hits:
        fail(f"semantic scenarios contain raw transcript fields: {raw_field_hits}")
    return {
        "path": str(path),
        "count": len(data),
        "ids": sorted(ids),
    }


def cli_auth_probe_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"mode": os.environ.get("LIVE_CLI_MODE", "probe"), "status": "not-run"}
    data = json.loads(read_text(path))
    return data["cli_auth_probe"]


def marker_counts(platform: str, path: Path) -> dict[str, Any]:
    data = json.loads(read_text(path))
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    counts = {
        "platform": platform,
        "path": str(path),
        "adapter_marker_count": text.count("[adapter:autopilot-mode] continue"),
        "legacy_stop_inject_marker_count": text.count("[autopilot] stop-inject"),
        "legacy_reset_count_marker_count": text.count("[autopilot] reset-count"),
        "legacy_inject_script_count": text.count("inject_autopilot_stop.py"),
        "legacy_reset_script_count": text.count("reset_inject_count.py"),
    }
    if counts["adapter_marker_count"] != 1:
        fail(f"{platform} adapter marker count is {counts['adapter_marker_count']}")
    for key, value in counts.items():
        if key.startswith("legacy_") and value != 0:
            fail(f"{platform} {key} is {value}")
    return counts


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_tasks(path: Path) -> None:
    item = {
        "id": "fresh-smoke",
        "status": "ready",
        "focus_layer": "macro",
        "depends_on": [],
        "prompt": "Container fresh install semantic smoke",
        "acceptance_criteria": ["semantic smoke criterion"],
        "allowed_surface": ["repo/file.txt"],
        "completion": {
            "state": "not_started",
            "verdict": None,
            "evidence": [],
            "completion_check_digest": None,
            "reopen_target": None,
        },
        "attempt": 0,
    }
    path.write_text(json.dumps(item, sort_keys=True) + "\n", encoding="utf-8")


def run_installed_adapter_smoke(platform: str, skill_dir: Path) -> dict[str, Any]:
    script = skill_dir / "adapters" / "autopilot_mode.py"
    if not script.is_file():
        fail(f"{platform} adapter script missing: {script}")
    with tempfile.TemporaryDirectory(prefix=f"{platform}-autopilot-smoke-") as tmp:
        run_dir = Path(tmp)
        write_json(
            run_dir / "approved-run.json",
            {
                "schema_version": "autopilot-run.v1",
                "run_id": f"{platform}-fresh-smoke",
                "approved": True,
                "status": "running",
                "scope": {"summary": "container fresh install semantic smoke"},
                "budget": {"remaining_steps": 2},
                "allowed_surfaces": ["repo/..."],
                "stop_conditions": ["budget_exhausted", "user_stop"],
                "approval_evidence": {"decision": "GO", "source": "fresh-e2e"},
            },
        )
        write_tasks(run_dir / "tasks.jsonl")
        env = dict(os.environ)
        env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
        result = subprocess.run(
            [sys.executable, str(script)],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            fail(f"{platform} adapter returned {result.returncode}: {result.stderr}")
        payload = json.loads(result.stdout.strip())
        if "work-item: fresh-smoke" not in payload.get("systemMessage", ""):
            fail(f"{platform} adapter did not surface fresh-smoke work item")
        tasks = [
            json.loads(line)
            for line in (run_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if tasks[0]["status"] != "running":
            fail(f"{platform} adapter did not mark task running")
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if [event.get("event") for event in events] != ["continue_next_item"]:
            fail(f"{platform} adapter events mismatch: {events}")
        return {
            "platform": platform,
            "skill_dir": str(skill_dir),
            "payload_contains_work_item": True,
            "task_status": tasks[0]["status"],
            "events": [event.get("event") for event in events],
        }


require_status("claude", Path("/work/status-claude.txt"))
require_status("codex", Path("/work/status-codex.txt"))
summary = {
    "semantic_scenarios": semantic_scenario_summary(Path("/work/live-e2e-scenarios.json")),
    "cli_auth_probe": cli_auth_probe_summary(Path("/work/cli-auth-probe.json")),
    "status": {
        "claude": "ok",
        "codex": "ok",
    },
    "markers": [
        marker_counts("claude", Path("/fresh-home/.claude/settings.json")),
        marker_counts("codex", Path("/fresh-home/.codex/hooks.json")),
    ],
    "smoke": [
        run_installed_adapter_smoke("claude", Path("/fresh-home/.claude/skills/autopilot-mode")),
        run_installed_adapter_smoke("codex", Path("/fresh-home/.agents/skills/autopilot-mode")),
    ],
}
print(json.dumps({"fresh_install_e2e": summary}, ensure_ascii=False, indent=2, sort_keys=True))
PY

emit_section "done"
'''
    return script.replace(
        "__SEMANTIC_SCENARIOS_JSON__",
        json.dumps(semantic_scenarios(), ensure_ascii=True, indent=2, sort_keys=True),
    )


def docker_run_args(
    *,
    image_tag: str,
    core_repo: Path,
    addon_repo: Path,
    run_script: Path,
    agent_cli_mode: str = "probe",
    live_cli_mode: str = "probe",
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-e",
        f"AGENT_CLI_MODE={agent_cli_mode}",
        "-e",
        f"LIVE_CLI_MODE={live_cli_mode}",
        "-v",
        f"{core_repo.resolve()}:/src/ghost-alice:ro",
        "-v",
        f"{addon_repo.resolve()}:/src/ghost-alice-autopilot:ro",
        "-v",
        f"{run_script.resolve()}:{CONTAINER_SCRIPT_PATH}:ro",
        image_tag,
        "bash",
        CONTAINER_SCRIPT_PATH,
    ]


def run_command(args: Sequence[str], *, cwd: Path | None = None) -> int:
    process = subprocess.run(list(args), cwd=str(cwd) if cwd else None)
    return int(process.returncode)


def build_image(image_tag: str, *, no_cache: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="ghost-alice-fresh-install-build-") as tmp:
        context = Path(tmp)
        (context / "Dockerfile").write_text(dockerfile(), encoding="utf-8")
        args = ["docker", "build", "-t", image_tag]
        if no_cache:
            args.append("--no-cache")
        args.append(str(context))
        return run_command(args)


def write_container_script(directory: Path) -> Path:
    script_path = directory / "run-inside-container.sh"
    script_path.write_text(container_script(), encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-repo", type=Path, default=default_core_repo())
    parser.add_argument("--addon-repo", type=Path, default=repo_root())
    parser.add_argument("--image-tag", default=DEFAULT_IMAGE_TAG)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--agent-cli-mode",
        choices=("skip", "probe", "required"),
        default="probe",
        help=(
            "skip avoids external CLI install; probe records Claude/Codex CLI install "
            "status without failing; required fails if CLI install or lookup fails"
        ),
    )
    parser.add_argument(
        "--live-cli-mode",
        choices=("skip", "probe", "required"),
        default="probe",
        help=(
            "skip avoids CLI auth probes; probe records Claude/Codex auth status "
            "without failing; required fails when auth status commands fail"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    core_repo = args.core_repo.expanduser().resolve()
    addon_repo = args.addon_repo.expanduser().resolve()
    if not (core_repo / "install.sh").is_file():
        sys.stderr.write(f"core repo install.sh not found: {core_repo}\n")
        return 2
    if not (addon_repo / "addons-manifest.json").is_file():
        sys.stderr.write(f"addon repo manifest not found: {addon_repo}\n")
        return 2

    if not args.skip_build:
        build_rc = build_image(args.image_tag, no_cache=args.no_cache)
        if build_rc != 0:
            return build_rc

    with tempfile.TemporaryDirectory(prefix="ghost-alice-fresh-install-script-") as tmp:
        run_script = write_container_script(Path(tmp))
        return run_command(
            docker_run_args(
                image_tag=args.image_tag,
                core_repo=core_repo,
                addon_repo=addon_repo,
                run_script=run_script,
                agent_cli_mode=args.agent_cli_mode,
                live_cli_mode=args.live_cli_mode,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
