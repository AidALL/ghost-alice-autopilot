# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

Official Ghost-ALICE addon for completion recording and approved task continuation.

Language: English | [Korean](./README_ko.md)

Ghost-ALICE Autopilot 0.5.0 requires Ghost-ALICE Core 0.5.0+ with the current response-route, completion and shared SQLite APIs, plus Python 3.11+. Installation grants no execution authority.

## Responsibilities

The current-session assistant is the model replying in the native conversation identified by the current hook receipt. A delegated assistant exists only for an explicitly delegated subtask. Ghost-ALICE Core and Ghost-ALICE Autopilot are responsibility areas containing instructions and Python code; neither is another reasoning agent.

The current-session assistant follows Ghost-ALICE instructions to compare claims with original evidence and judge the complete answer against the user objective and constraints. It requests the actual shell, Python and business tool calls through the native host. Ghost-ALICE code saves the review inputs and authenticates the assistant's recorded judgments. The assistant then follows Ghost-ALICE Autopilot instructions to decide whether necessary authorized work remains; Ghost-ALICE Autopilot adapter code records supported results and deterministically selects tasks from the stored approved plan.

| Actual actor | Input | Action | Output |
| --- | --- | --- | --- |
| Current-session assistant | Hook receipt, user authority and model-reviewed results with authenticated Ghost-ALICE bindings | Follow task-router, boundary-contract and verification-before-completion; use Ghost-ALICE Autopilot instructions to judge remaining work and request work tools | Business results, original criterion proof and an approved continuation plan or partial response |
| Ghost-ALICE Core validation code | Recorded assistant review and exact bindings | Authenticate review/source bindings, admission and delivery prerequisites | Completion assessment and response guidance |
| Ghost-ALICE Autopilot adapter code | Ghost-ALICE Core completion assessment and approved plan | advance_continuation selects an eligible task, records dispatch and builds feedback | Continuation payload; _format_payload_for_hook returns block/reason when required |
| Native host | Assistant tool requests and hook feedback | Enforce permission, invoke tools/hooks and provide returned feedback to its addressed conversation when supported | Tool results and feedback for the current-session assistant; visible response for the user |

~~~mermaid
flowchart TD
  U[User states the objective, constraints and actual approval] -->|Objective, constraints and granted authority| S[Responding model selects micro, meso, macro or meta focus<br/>and independently selects verification depth]
  S -->|Authorized tool request| T[Native host runs permitted tools and returns actual results]
  T -->|Original results and source locations| D[Responding model drafts the exact complete answer and identifies original sources]
  D -->|Host shell-tool request: prepare-response with response-file and source paths| HP[Native host invokes Ghost-ALICE completion_assessment.py]
  HP -->|response_text and source_paths read from supplied files| E[Ghost-ALICE prepare_response_review saves the exact inputs<br/>and returns candidate_reference, bindings, sources and response_binding]
  E -->|Exact draft, original source copies and reference| V[Responding model judges each claim against original evidence in stage 1]
  V -->|Individual claim judgments and exact draft| V2[Responding model judges the exact whole answer in stage 2<br/>using stage 1 judgments, the objective and constraints]
  U -->|Objective and required conditions| V2
  V2 -->|Host shell-tool request: record-response with candidate reference and judgment-file| HR[Native host invokes Ghost-ALICE completion_assessment.py]
  HR -->|Saved candidate reference and claims plus response judgments| C[Ghost-ALICE record_response_review authenticates bindings and stores both judgments atomically<br/>including valid unsupported outcomes]
  C -->|Authenticated review results and remaining requirements| J[Responding model applies Ghost-ALICE Autopilot instructions<br/>to judge whether authorized remaining work is needed]
  U -->|Actual approval and its boundaries| J
  U -->|Already authorized conditional task plan| P
  J -->|Needed work within actual authority| P[Responding model registers exact tasks and dependencies before Stop]
  J -->|No justified authorized action| N[Responding model retains unresolved requirements or omits nonessential work]
  P -->|Host Python-tool request with current source/event, items, actual approval and allowed surfaces| HT[Native host imports installed autopilot_continuation and calls register_continuation_plan]
  HT -->|Validated plan inputs| Q[Ghost-ALICE Autopilot stores the approved plan<br/>and returns admitted plus plan_id]
  C -->|Both review stages supported: exact response_text| R[Responding model submits the reviewed answer to the native host]
  C -->|Unsupported or unavailable outcome: no response_text| N
  R -->|Submitted answer and current hook receipt| K[Native host invokes the registered completion guard and Ghost-ALICE Autopilot Stop hook independently]
  K -->|Completion-guard invocation with answer and receipt| G[Ghost-ALICE Core completion guard authenticates the submitted answer against stored review]
  K -->|Ghost-ALICE Autopilot Stop invocation with answer and receipt| B[Ghost-ALICE Autopilot apply_completion_v2 publishes only Core-selected completion proof<br/>and applies or reuses authenticated publications]
  C -->|Stored review and exact evidence bindings| G
  C -->|The same stored review and exact evidence bindings| B
  B -->|Applied criterion status and recording outcome| A[Ghost-ALICE Autopilot advance_continuation selects eligible approved work from the plan]
  Q -->|Stored tasks, approval boundaries and dependencies| A
  A -->|Eligible task and unchanged-state dispatch check| F[Ghost-ALICE Autopilot code records dispatch and formats Stop feedback]
  A -->|No eligible task and recorded state| W[Ghost-ALICE Autopilot code returns complete, waiting or parked status]
  F -->|Task request addressed to the hook receipt's conversation| H[Native host receives the addressed Stop feedback]
  W -->|Continuation status for the hook receipt's conversation| H
  H -->|If the host delivers the task request to that conversation| X[Model generating that conversation's next response receives the task request]
  X -->|Received task request under existing authority| S
  R -->|Exact submitted answer| O[Native host handles visible answer delivery]
  G -->|Completion-guard result| O
  H -->|Stop continuation result| O
  C -.->|Review recording events| L[Audit code records full events]
  A -.->|Continuation and dispatch events| L
~~~

The [Ghost-ALICE Core architecture](https://github.com/AidALL/ghost-alice/blob/main/architecture.md) defines completion ownership. The [operating skill](./addons/autopilot-mode/skill/SKILL.md) defines Ghost-ALICE Autopilot commands, record schemas and target selection.

The [comparison diagram](https://github.com/AidALL/ghost-alice/wiki/design-philosophy-floor-not-ceiling#comparison-and-feedback) and [dynamic focus diagram](https://github.com/AidALL/ghost-alice/wiki/operating-model-fallback-and-escalation#focus-and-verification-burden) govern the assistant's judgment about remaining work. The assistant may reconsider a task's necessity or premise without asking the user; it asks when the next action needs a missing decision that belongs to the user. An unresolved detail does not require a recovery task when the objective and required dependencies are unaffected. Required unsupported criteria stay unresolved. Ghost-ALICE Autopilot consumes Ghost-ALICE Core's result and the stored approved plan instead of repeating the claim's meaning review or creating new authority. The runtime diagram above separates planning before Stop from deterministic selection at Stop; Stop does not invoke the model to invent a missing plan. Focus and verification burden remain independent.

Ghost-ALICE Core stores the review records; Ghost-ALICE Autopilot stores publication, plan and dispatch records separately. The native host invokes the completion guard and Ghost-ALICE Autopilot Stop hook independently, and both consume the shared Ghost-ALICE Core assessment. Neither code path performs the assistant's business work. The hook receipt identifies the conversation; conditional feedback delivery reaches the model generating that conversation's next response and does not promise resumption of the same model process.

## Install and remove

The operator runs the Ghost-ALICE Core installer from the Ghost-ALICE Core checkout:

~~~bash
bash install.sh --addon autopilot
bash install.sh --platform codex --addon autopilot
~~~

The operator selects Claude with --platform claude. For a development checkout, the operator uses bash `<ghost-alice>/install.sh` --addon-source `<addon-repository>`. Installation status uses bash `<ghost-alice>/install.sh` --platform codex --status.

The Ghost-ALICE Core installer registers Ghost-ALICE Autopilot adapters, checks their source/hash bindings and installs hooks; Ghost-ALICE Autopilot has no standalone installer. The [Ghost-ALICE Core installation guide](https://github.com/AidALL/ghost-alice/blob/main/docs/getting-started/installation.md) describes platform setup.

The operator removes the addon with bash `<ghost-alice>/install.sh` --platform codex --uninstall --addon autopilot-mode, substituting --platform claude for Claude. Full Ghost-ALICE Core removal uses bash `<ghost-alice>/install.sh` --uninstall.

## Completion recording and delivery

The current-session assistant selects current authorized work through task-router and follows the [Core response review and publication procedure](https://github.com/AidALL/ghost-alice/blob/main/coding-convention/verification-before-completion/references/autopilot-publication.md). That owner defines prepare-response and record-response calls, the separate claim and whole-answer judgments, response_text submission, original proof/time retention and delivery.

The registered pretool prepares recording provenance for selected work. At native Stop, Ghost-ALICE Autopilot adapter code consumes Core's authenticated review, records publication and requests guarded criterion writes. completion_recording describes recording and criterion application; continuation describes the approved plan's task state. Publication acceptance, partial application, whole-task completion and visible delivery are distinct results. A recording failure parks the affected dependent record; required correction and delivery follow the Core owner procedure.

## Approved continuation and queues

The model in the current conversation judges necessary follow-up work from reviewed results and actual user authority. It can also register an already authorized conditional plan before the current result's Stop application. [Register the approved plan](./addons/autopilot-mode/skill/references/continuation.md#register-the-approved-plan) defines register_continuation_plan inputs and the native host Python call. Ghost-ALICE Autopilot advance_continuation selects eligible work from applied criteria and dependencies, then records dispatch. If the host delivers its feedback to the receipt's conversation, the model generating that conversation's next response performs the authorized work. Ghost-ALICE Autopilot code does not execute business tools or spawn assistants.

Explicit v1 queue callers use the [Approved queue API](./addons/autopilot-mode/skill/SKILL.md#approved-queue-api). Admission needs actual approval evidence, current receipt, boundary and budget; [Consistency decisions](./addons/autopilot-mode/skill/SKILL.md#consistency-decisions) distinguishes the selected runtime protocols.

## State, targets and pause controls

SQLite and public storage APIs are authoritative. JSON exports and inboxes retain their documented import/export roles; editing an export cannot change stored authority. Ghost-ALICE Core source modules, runtime modules, ledger, project and installed skills have explicit separate locations. Invalid bindings fail without searching another authority.

Managed v2 sessions with trusted absolute GHOST_ALICE_GHOST_ROOT default to `<ghost-root>/autopilot/sessions/<platform>/<session-id>/`. Existing exact-owner receipts retain the original target after cwd changes. The current-session assistant uses returned automatic_target.run_dir or receipt run_dir; strict GHOST_ALICE_AUTOPILOT_RUN_DIR and absolute GHOST_ALICE_AUTOPILOT_CWD overrides follow the [target contract](./addons/autopilot-mode/skill/SKILL.md#target-and-state-inspection).

| Pause marker | Run affected |
| --- | --- |
| `<project>/.autopilot/OFF` | Default runs for the business context |
| `<ghost-root>/autopilot/OFF` | Managed runtime runs |
| `<selected-run-dir>/OFF` | Selected run |

Ghost-ALICE Autopilot adapter code honors pause markers during pretool, preparation, publication and Stop. On an authorized resume, the current-session assistant removes the applicable marker; remaining markers still apply. On explicit stop, the current-session assistant retains the marker and records approved-queue status stopped through the public transaction API.

## Compatibility and repository layout

[compatibility-matrix.json](./compatibility-matrix.json) records target evidence. A not-run target remains unverified; source checks establish neither installation nor native host adoption.

- addons-manifest.json: addon registration and minimum Ghost-ALICE Core version.
- addons/autopilot-mode/skill/SKILL.md: operating contract.
- addons/autopilot-mode/skill/adapters/: recording and continuation implementation.
- addons/autopilot-mode/skill/scripts/: completion, queue and governance entrypoints.
- scripts/autopilot_session_bridge.py: repository bridge wrapper.
- tests/: source contracts and behavior checks.

## License

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
