# Approved task continuation

Load this reference when the current-conversation model has identified necessary authorized follow-up work or must register an already authorized conditional dependency plan. The model makes that necessity and authority judgment; the Ghost-ALICE Autopilot API records the supplied plan. Admission checks the current selected criteria and boundary without requiring a completed response review or Stop application. Execution eligibility remains conditional on the recorded dependencies and applied criteria.

Core verification-before-completion owns claim/source and whole-answer review. Resolve its installed references/autopilot-publication.md when preparing a response with new claims. Use this reference for the Ghost-ALICE Autopilot plan handoff.

## Register the approved plan

An unmet criterion or successful publication is not an executable plan. The current-session assistant registers only executable steps already authorized by the human. Resolve `AUTOPILOT_SKILL_ROOT` from the trusted installed Ghost-ALICE Autopilot binding; its `adapters/autopilot_continuation.py` imports sibling modules, so add the `adapters` directory itself to Python's module path.

The assistant prepares a JSON object in permitted runtime scratch at absolute `PLAN_INPUT`. Its `expected_input_event_id` is the current canonical receipt/state event, not an old event attached to a new decision. Its `approval_evidence` is `{"decision":"approved","source":"<compressed actual human authorization and locator>"}`; `approve` and `go` are also accepted decision values, but the assistant cannot invent approval. Its `allowed_surfaces` is a nonempty string list of the actual approved boundaries. Each `items` element contains:

- `id`, `status`, `focus_layer` and `prompt`: a stable nonempty task ID, its actual current task state, the selected focus and a self-contained executable instruction for the receiving assistant. Do not admit an item as `completed` or `not_applicable`; use `blocked` for a missing required decision and `stopped` for an explicit stop.
- `allowed_surface`: nonempty strings within the approved `allowed_surfaces`; `depends_on`: known task IDs forming an acyclic dependency graph; `acceptance_criteria`: criterion descriptions. The latter two may be empty lists.
- `criterion_refs`: nonempty qualified references `{work_unit_id, contract_revision, criterion_id}` copied from the exact current selected, admitted, owned contract. No reference may occur twice within or across items. Omit `attempt` for its initial zero default; do not manufacture completion metadata.

The native host execution tool must retain the current trusted managed runtime environment, including its Core/runtime/owned roots, platform/project context, native v2 capability and any exact existing `GHOST_ALICE_AUTOPILOT_RUN_DIR` or `GHOST_ALICE_AUTOPILOT_CWD` overrides. CORE_ROOT is the trusted runtime GHOST_ALICE_CORE_ROOT (or supplied GHOST_ALICE_SKILLS_DIR fallback); LEDGER_ROOT, PLATFORM, SESSION_ID and the current event come from the exact hook receipt and canonical state. Those receipt bindings must match the runtime context. A missing compatible context leaves admission unavailable; creating a foreign run or inventing overrides is not recovery.

After binding those paths and receipt coordinates, use the exposed shell tool:

```bash
GHOST_ALICE_CORE_ROOT="$CORE_ROOT" \
GHOST_ALICE_SESSION_INTENT_ROOT="$LEDGER_ROOT" \
GHOST_ALICE_PLATFORM="$PLATFORM" \
GHOST_ALICE_SESSION_ID="$SESSION_ID" \
python3 - "$AUTOPILOT_SKILL_ROOT" "$PLAN_INPUT" <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'adapters'))
from autopilot_continuation import register_continuation_plan
plan = json.loads(Path(sys.argv[2]).read_text())
result = register_continuation_plan(dict(os.environ),
    expected_input_event_id=plan['expected_input_event_id'],
    items=plan['items'], approval_evidence=plan['approval_evidence'],
    allowed_surfaces=plan['allowed_surfaces'])
print(json.dumps(result))
PY
```

The API validates current selection, item references, surfaces and dependencies, persists the admitted queue in the resolved Ghost-ALICE Autopilot runtime target, and returns `{"status":"admitted","plan_id":"sha256:..."}`. The assistant retains that returned ID. An identical plan preserves its dispatch state; an invalid binding or shape raises an error and admits no plan. Read the exit status/error rather than claiming admission from the proposed JSON. If scratch files are prohibited, the exposed Python tool may pass the same constructed object directly to this API; it does not waive any origin or schema requirement.

After privileged v2 application, `advance_continuation` maps accepted proof to completed tasks, selects dependency-ready work from the recorded approved queue and records dispatch. Its payload contains `continuation: {status: "dispatched", task_id, criterion_refs, plan_id}` and a `systemMessage` containing the task's prompt, allowed surfaces and criterion references. At Stop, `_format_payload_for_hook` transports that message in stdout JSON as `decision: "block"` with `reason` equal to `systemMessage`. The native host receives this continuation request; if it delivers the request to the conversation that triggered the hook, the model generating that conversation's next response executes the assigned step. The Stop input's session ID identifies the destination; it does not establish reuse of a model process or create a delegated assistant. An already approved plan can consume existing accepted proof during a non-closing partial or explanation response without creating a new publication obligation. Recording-only parked errors do not request this continuation block.

A dispatch record does not prove that the host executed the next action. The current-session assistant receiving that feedback executes and verifies the requested action under host permissions and the selected skill instructions. Actual proof invalidation reopens only affected completed tasks and dependency checks, including a running descendant. A blocked branch does not cancel independent work.

The same unchanged task/criterion state dispatches once, then waits for relevant progress or an explicitly revised plan. A new input alone does not reset that condition. Keep `continuation` distinct from `completion_recording`.
