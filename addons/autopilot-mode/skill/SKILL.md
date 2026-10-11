---
name: autopilot-mode
description: "Use when approved Ghost-ALICE run state or current-session runtime material requires completion recording or continuation of authorized work."
compatibility:
  - "Python 3.11+ standard library"
  - "Ghost-ALICE Core 0.5.0+ with response-route, completion and shared SQLite APIs"
  - "Claude Code or Codex hooks registered by the Ghost-ALICE Core installer"
---

# Ghost-ALICE Autopilot

The current-session assistant is the model replying in the native conversation identified by the current hook receipt. A delegated assistant is a separate model assigned an explicit subtask; Ghost-ALICE Autopilot continuation does not create one. Ghost-ALICE Core and Ghost-ALICE Autopilot are responsibility areas containing instructions and Python code, not separate reasoning agents.

The current-session assistant follows Ghost-ALICE instructions to judge claims, original evidence and authority. It then follows Ghost-ALICE Autopilot instructions to decide whether necessary authorized work remains. The assistant requests shell, Python or business tools through the native host and reads their returned results. Ghost-ALICE code authenticates and stores the assistant's recorded judgments; Ghost-ALICE Autopilot Python code consumes that authenticated review, records proof and selects eligible tasks from an approved dependency plan. Installing Ghost-ALICE Autopilot grants no execution authority.

## Actors and critical rules

| Actual actor | Input | Action | Output |
| --- | --- | --- | --- |
| Current-session assistant | Current native hook receipt, user objective/authority, returned tool results and saved response/source snapshots | Request the host tools that perform the authorized work; judge claims against original sources, then review the whole answer; submit both judgments through the Ghost-ALICE review API; decide and register necessary authorized continuation | Actual work results, recorded review judgments and an admitted dependency plan or honest partial response |
| Ghost-ALICE Autopilot adapter code | Ghost-ALICE Core completion assessment and approved dependency plan | advance_continuation selects a dependency-ready task, records dispatch and builds feedback; autopilot_mode._format_payload_for_hook formats native Stop feedback | Continuation status and, when applicable, block/reason for the native host |
| Ghost-ALICE validation code | Exact response draft/source paths for preparation; candidate reference and the assistant's two judgments for recording; current receipt/route bindings | Save immutable review inputs, authenticate bindings and store the distinct judgments atomically; provide the current completion assessment | Prepared candidate/source contents; reviewed response_text when supported; publication_route and delivery guidance |
| Native host | Assistant tool requests and hook payloads | Enforce tool permission, run registered hooks and provide returned hook feedback to its addressed conversation when supported | Tool results and continuation feedback for the current-session assistant; visible responses for the user |

Ghost-ALICE Autopilot adapter code consumes the Ghost-ALICE Core completion service's publication_route: none | prepare | publish | blocked. Ghost-ALICE Autopilot adapter code does not classify final prose, validate retained claims or define delivery requirements. Publication acceptance, criterion application, whole-task completion, task dispatch and visible delivery are distinct results. A missing record is not a failed business outcome.

The current-session assistant preserves human approval, credentials, owner/source/hash identity, current lineage and strict audit for every operation. Neither Ghost-ALICE Core nor Ghost-ALICE Autopilot recording errors revoke native tool permission. The registered adapter accepts no arguments and rejects argv with exit code 64.

## Managed completion recording

The current-session assistant loads installed verification-before-completion and its references/autopilot-publication.md from the trusted runtime skill directory. That Core reference owns response review, publication, original-source/time handling, accepted-record correction and delivery. Its Prepare and record the response review section defines the exact draft/source inputs, two distinct model judgments and host-tool calls; Native publication and manual CLI defines explicit publication callers. The current hook receipt supplies platform, session, ledger root and input event. The runtime supplies the Core API root and installed Ghost-ALICE Autopilot directory.

The registered pretool authenticates the current Core selection and captures recording provenance. Capture failure returns continue: true and records the unavailable prerequisite. At native Stop, apply_completion_v2 consumes Core's authenticated review, records supported proof under the current receipt and original capture, and requests guarded criterion writes. Publication acceptance and criterion application are separate results. The assistant follows the Core owner procedure for its claims; native Stop owns publication for this managed path.

## Recording failure and delivery

Ghost-ALICE Autopilot records a recording failure and parks dependent application until a relevant state change. A recording-only error does not block Stop or request answer resubmission. Separately approved ready work remains eligible. For a required record correction, the current-session assistant follows the Core reference's Recording-only failures section and its named authority, incident-accounting and verification owners. The reference's Apply and deliver section owns response delivery guidance.

## Consistency decisions

The bound runtime protocol selects one path. With `autopilot-completion-receipt.v2`, the registered Stop adapter calls `apply_completion_v2` and `advance_continuation`; use Managed v2 task continuation below. The Approved queue API below belongs to an explicitly selected legacy v1 caller. Its action inboxes do not drive native v2 continuation. Preserve the protocol supplied by the trusted runtime; a missing queue file does not select v1, and an unknown runtime binding remains unresolved.

### Managed v2 task continuation

The current-conversation model uses the reviewed results, remaining objective and actual user authority to judge whether necessary work remains. It may also register an already authorized conditional dependency plan before the current result is applied. It follows [Register the approved plan](references/continuation.md#register-the-approved-plan) for the complete source/event bindings, task schema, host Python call and result branches. The returned admitted status records a plan; it does not execute a task.

After apply_completion_v2, advance_continuation maps applied criteria to plan status, selects dependency-ready approved work and records dispatch. The native Stop formatter returns that task request to the host for the hook receipt's conversation. If the host delivers it, the model generating that conversation's next response requests the authorized work tools. Ghost-ALICE Autopilot code does not execute business tools or create an assistant. The reference owns unchanged-state dispatch and dependent-task handling.

### Approved queue API

The current-session assistant uses the queue API only when the authorized objective includes queue admission or execution. Managed completion-recording correction requires no queue, consistency-decision.json, conduct-plan inbox or approval_generation.

1. Through the host shell tool, the assistant runs `python3 AUTOPILOT_SKILL/scripts/autopilot_session_bridge.py --intent-root ROOT --platform PLATFORM --session-id SESSION --run-dir RUN_DIR --check`. AUTOPILOT_SKILL is the installed skill directory from the current runtime; ROOT, PLATFORM and SESSION come from the current receipt; RUN_DIR is the exact selected run target. The check writes no run state. Read latest_input_event.event_id and automatic_target.run_dir/matches_requested_run from its JSON result; activation_guaranteed is false. A custom directory does not redirect the host Stop target.
2. Queue admission requires the approval input receipt and explicit approval evidence with a nonempty source matching platform, session, owner and objective. Using the same script and exact coordinates, the assistant replaces --check with `--input-event-id INPUT --current-work-item-id ITEM --plan-path PLAN --approval-evidence-json APPROVAL --remaining-steps STEPS --allowed-surface SURFACE --stop-condition CONDITION`; repeat the last two flags for additional approved entries. INPUT is the checked event under which approval was granted; ITEM identifies the authorized task; PLAN is its approved plan path; APPROVAL is JSON containing decision and the actual authorization source; STEPS, surfaces and stop conditions come from that authorization. Read mode, writes_state, approval_generation and automatic_target in the result. A runnable queue has approved: true, status: running, positive budget.remaining_steps, scope, allowed surfaces, stop conditions and approval evidence. Import uses approved-run.json (autopilot-run.v1) and tasks.jsonl; subsequent changes use public storage APIs.
3. The current-session assistant preserves task records and selects ready/reopened tasks only after dependencies are satisfied. Task statuses are ready, running, completed, reopened, blocked, stopped and not_applicable. Session-bound evidence and promoted actions retain the approval_generation under which the evidence was captured. Changed input or contract requires bridge authorization evaluation.
4. Through the host shell tool, the assistant runs `python3 AUTOPILOT_SKILL/scripts/autopilot_governance_signal.py decision-candidate --work-item-id ITEM --approval-generation GENERATION --out CANDIDATE` with the applicable original --intent-state, --routing-surface, --completion-validation or --governance-signal JSON path. ITEM and GENERATION come from the selected authoritative queue; CANDIDATE is its consistency-decision.candidate.json output path. The helper computes candidate IDs and state/evidence/loop digests from those supplied inputs. For an eligible candidate, run the same script with `promote-decision --candidate CANDIDATE --run-dir RUN_DIR --out RUN_DIR/consistency-decision.json`. Exit 0 means the action file was written; exit 3 means no eligible action was produced. observation_signal candidates remain diagnostic. The separate conduct-plan-candidate command reads --intent-state, --current-work-item-id, --plan-path and --approval-generation and writes --out conduct-plan.candidate.json; promote-conduct-plan consumes --candidate and actual --approval-evidence-json and writes --out RUN_DIR/conduct-plan.json.
5. Before Stop, the current-session assistant follows the returned queue before-stop instructions. When a completion/retry/reopen action is resolved, it promotes the eligible candidate or writes the complete consistency-decision.json record in the selected run directory using the existing direct-decision schema below. A direct record cites the actual current-turn decision and original evidence; its state_hash identifies the retained decision inputs, governance_signal_digest identifies that evidence, decision_key identifies the resolved action and loop_key identifies the same action/state pair across attempts. Preserve an existing value for unchanged input and do not borrow a diagnostic candidate's identity or approval generation. Missing decision records permit resuming the same item from current runtime material; the current-session assistant asks the user only for an unresolved human-owned decision.

| Record | Required contract |
| --- | --- |
| consistency-decision.json | schema_version: autopilot-consistency-decision.v1; promotion_state: promoted; decision_id, decision, work_item_id, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, state_hash, decision_key, loop_key and evidence. evidence is an array of strings. |
| promotion_evidence.decision | go, approve, approved, promote, promoted or direct. direct means a current-turn resolved action without a promotable candidate; all schema and verification requirements still apply. |
| decision | continue_next, retry_same_unit, reopen_micro, reopen_meso, reopen_macro, reopen_meta, park, ask_user_meta or stop. continue_next accepts running/ready/reopened tasks; other decisions require running. |
| continue_next proof | Final all-pass criterion-bound [completion-check], unverified: none, top-level verdict: pass and completion_check_digest: `sha256:<64-hex>`. A partial-check cannot advance the whole item. |
| conduct-plan.json | schema_version: autopilot-conduct-plan.v2; promotion_state: approved; approval evidence, source candidate ID and evidence digest. Proposals retain proposal_status: proposed and approval_required: true; the approval transition copies task_template as ready. Import preserves existing task IDs and observer requirements. |

The current-session assistant follows task-router to choose the next judgment focus independently of verification depth, boundary-contract for authority and systematic-debugging for incident limits. It records the resulting queue action with the schema above. Repeated unchanged decisions or exhausted local retry allowance produce park: Ghost-ALICE Autopilot marks only the implicated item blocked with completion.state: parked. Dependents wait while independent approved work remains eligible.

Ghost-ALICE Autopilot storage code consumes action inboxes in the same SQLite transaction as the guarded Ghost-ALICE Core criterion writes. Rejected or unavailable proof leaves unfinished work intact. Consumed receipts prevent replay; after-commit archiving preserves concurrent producer writes.

## Target and state inspection

The current-session assistant uses the returned automatic_target.run_dir or receipt run_dir for inspection and recording. Pretool, preparation/publication and Stop share the following target selector:

- A managed v2 session with trusted absolute GHOST_ALICE_GHOST_ROOT defaults to `<ghost-root>/autopilot/sessions/<platform>/<session-id>/` before capture, regardless of business-directory writability. An existing exact-owner target from immutable receipts or known project/runtime descriptors retains the original target after cwd changes. Missing/changed bound descriptors or ambiguous targets require correction of the identified binding, not a new proof origin.
- Without trusted runtime binding, Ghost-ALICE Autopilot target-selection code retains the legacy project-session default; without supplied identity, Ghost-ALICE Autopilot target-selection code uses the project default. Ghost-ALICE Autopilot target-selection code does not open a foreign authority database for target discovery.
- GHOST_ALICE_AUTOPILOT_RUN_DIR strictly overrides the run directory; absolute GHOST_ALICE_AUTOPILOT_CWD is the next project-root override. A relative explicit project-root override produces a blocking adapter reason.
- Without overrides, for Claude, Ghost-ALICE Autopilot target-selection code uses the first nonempty absolute CLAUDE_PROJECT_DIR or hook cwd. For Codex, Ghost-ALICE Autopilot target-selection code uses hook cwd and ignores inherited CLAUDE_PROJECT_DIR. For both platforms, Ghost-ALICE Autopilot target-selection code falls back to absolute process cwd and skips relative derived candidates; an access failure after target selection does not select a lower-priority target.

The current-session assistant retains typed recording/adapter acquisition and processing errors. An ownership-conflict cannot authorize replacing another owner's approval. Invalid explicit Ghost-ALICE Core location produces unavailable status without authority discovery. Ghost-ALICE Core source APIs, runtime modules, authoritative ledger, project and installed skills are separate locations; transport grants no admission, approval, proof origin or native permission.

For queue inspection, the assistant uses the installed adapters import and current source bindings from the continuation handoff, then calls autopilot_storage.read(run_dir / "approved-run.json"), read(run_dir / "tasks.jsonl"), read(run_dir / "events.jsonl") and pending(action_path) through the host Python tool. The reads return the authoritative parsed run object or task/event list; pending returns whether the inbox still has an unconsumed action. SQLite and public APIs are authoritative; physically retained action files may already be consumed. JSON exports are not writable authority. The current-session assistant preserves the authority locator with the database; missing/corrupt initialized storage remains unavailable. The current-session assistant pursues recovery only when required authorized work depends on that storage.

For Ghost-ALICE Core inspection, the current-session assistant uses session_intent_ledger.py --read-state or --read-events with the exact root/platform/session. intent-state.json and intent-events.jsonl are import/export locators.

## Pause, resume and stop

| Marker | Effect |
| --- | --- |
| `<project>/.autopilot/OFF` | Pauses default runs for the business context. |
| `<ghost-root>/autopilot/OFF` | Pauses managed runtime runs. |
| `<selected-run-dir>/OFF` | Pauses the selected run. |

Ghost-ALICE Autopilot target-selection code checks known project/runtime pause markers before authority lookup; locating arbitrary bound targets requires the exact-session receipt. Pause markers apply to pretool, preparation/publication and Stop, including explicit commands. On an authorized resume, the current-session assistant removes the applicable marker; any remaining marker still pauses the corresponding run. On an explicit stop, the current-session assistant preserves the applicable marker. For an approved queue, it uses the same installed adapters/source binding through the host Python tool: `with autopilot_storage.transaction(run_dir, source) as store: run = store.run(); store.put_run(dict(run, status="stopped"))`. The transaction commits the changed run on successful exit while preserving the other approved fields. The current-session assistant replans through the validated bridge/work-item API while preserving terminal items and approval boundaries.

## Compatibility and installation

Before a compatibility claim, the current-session assistant reads repository compatibility-matrix.json. A not-run target remains unverified; source tests establish neither installed adoption nor live host behavior.

The operator runs the Ghost-ALICE Core installer from the Ghost-ALICE Core checkout:

~~~bash
bash install.sh --addon autopilot
bash install.sh --platform codex --addon autopilot
~~~

The operator selects Claude with --platform claude, installs a development checkout with bash `<ghost-alice>/install.sh` --addon-source `<addon-repository>`, or removes the addon with bash `<ghost-alice>/install.sh` --platform codex --uninstall --addon autopilot-mode. The Ghost-ALICE Core installer owns privileged_adapters: [autopilot-mode], registration, events, markers, runner namespace and adapter location. Ghost-ALICE Autopilot has no independent hook installer.
