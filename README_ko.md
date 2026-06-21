# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

승인된 자율 연속 실행을 위한 공식 Ghost-ALICE 애드온.

Language: [English](./README.md) | Korean

`autopilot-mode`는 Ghost-ALICE가 승인된 실행을 작업 단위로 이어가게 하는 애드온이다. 에이전트 stop 이벤트 이후 프로젝트의 `.autopilot/` 상태를 읽고, 다음 ready 또는 reopened 작업을 선택하며, current io-trace material이 있으면 unresolved running item을 재개하고 다음 continuation message를 출력한다.

## 이 애드온이 하는 일

- `autopilot-mode` skill을 설치한다.
- Ghost-ALICE installer를 통해 core-owned `[adapter:autopilot-mode] continue` hook을 등록한다.
- 프로젝트 로컬 `.autopilot/` 실행 상태를 읽는다.
- 명시적 승인 후 session-intent ledger file에서 `.autopilot/`을 부트스트랩하는 `skill/scripts/autopilot_session_bridge.py`와 repository wrapper `scripts/autopilot_session_bridge.py`를 제공한다.
- Stop adapter가 별도 receptor를 만들지 않고 session-intent와 io-trace 또는 open conduct feedback에서 current-session `.autopilot/` state를 materialize할 수 있다.
- `autopilot_governance_signal.py`로 evidence-backed governance candidate와 promotion을 만든다.
- 승인된 `conduct-plan.json` proposal queue를 durable `tasks.jsonl` work item으로 가져온다.
- no-op payload 또는 다음 work-item message를 출력한다.
- adapter event를 `.autopilot/events.jsonl`에 기록한다.

이 애드온은 current session 밖의 작업을 만들지 않는다. session-intent analysis, task routing, 사용자의 명시적 GO 결정, current-session runtime material이 approved run state를 만든다.

## 동작 방식

런타임 흐름:

1. Ghost-ALICE core installer가 이 애드온을 설치하고 privileged adapter hook을 배선한다.
2. 프로젝트는 사용자 승인 후 `.autopilot/approved-run.json`과 `.autopilot/tasks.jsonl`을 만든다. conduct-feedback 실행은 승인된 `.autopilot/conduct-plan.json`을 대신 제공할 수 있다. package bridge `skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper `scripts/autopilot_session_bridge.py`는 caller가 명시적 approval evidence를 제공할 때 `current-session.json`, `intent-state.json`, `intent-events.jsonl`에서 이 run state를 만들 수 있다. Stop adapter는 session-intent와 io-trace 또는 open conduct feedback이 runtime material을 제공할 때 current session을 materialize할 수도 있다.
3. 에이전트가 멈추면 adapter가 `.autopilot/`을 읽는다.
4. governance signal은 먼저 `consistency-decision.candidate.json` 또는 `conduct-plan.candidate.json`을 쓴다. 이 candidate file은 adapter-consumable이 아니다.
5. promotion만 adapter-consumable `consistency-decision.json` 또는 승인된 `conduct-plan.json`을 만든다.
6. `conduct-plan.json`이 있으면 adapter는 ready task 확인 전에 새 proposed queue item을 `tasks.jsonl`로 가져온다.
7. 실행이 approved, running, 예산 내 상태이고 ready 또는 reopened task가 있으면 adapter가 해당 task를 `running`으로 표시한다.
8. running task에 promoted decision이 없지만 current io-trace가 있으면 adapter는 io-trace를 `autopilot-observation-signal.v1`로 넣고 같은 task를 재개한다.
9. adapter가 다음 work item과 decision이 resolved되었을 때 `.autopilot/consistency-decision.json` 작성 또는 promotion을 요구하는 `before-stop` 지시가 담긴 continuation payload를 출력한다.
10. 실행이 승인되지 않았거나, pause/stop 상태이거나, 예산이 없거나, runnable item 또는 runtime material이 없으면 no-op payload를 반환한다.

기본 run directory:

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
  conduct-plan.candidate.json
  conduct-plan.json
  conduct-plan.applied.json
  consistency-decision.candidate.json
  consistency-decision.json
  consistency-decision.applied.json
  events.jsonl
  OFF
```

## Governance Candidate와 Promotion

`addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py`는 session intent, conduct feedback, routing-surface correction, completion validation failure를 evidence-backed candidate file로 변환한다. candidate file은 진단 출력일 뿐이다.

- `consistency-decision.candidate.json`은 `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용한다.
- `conduct-plan.candidate.json`은 `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용한다.
- candidate schema가 adapter-consumable 경로에 잘못 놓여도 adapter는 이를 거부한다.

promotion은 adapter-consumable file을 만드는 경계다. `promote-decision`은 `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, promotion evidence, candidate id, evidence digest, state hash, decision key, loop key를 포함한 `consistency-decision.json`을 쓴다. `promote-conduct-plan`은 `promotion_state: "approved"`, approval evidence, source candidate id, evidence digest를 포함한 승인 `conduct-plan.json`을 쓴다.

promotion command는 `--run-dir`로 `.autopilot/tasks.jsonl`과 `.autopilot/events.jsonl`을 읽을 수 있다. retry cap 또는 repeated decision/state loop가 확인되면 같은 결정을 반복하지 않고 `ask_user_meta`로 escalate한다.

## Session-Intent Bridge

설치만으로 `.autopilot/`은 생성되지 않는다. 현재 Ghost-ALICE session
ledger에서 approved run을 활성화하려면 package bridge
`skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper
`scripts/autopilot_session_bridge.py`를 사용한다. bridge는 `.tmp/session-intent/<platform>/current-session.json`,
그 pointer가 가리키는 `intent-state.json`, 같은 디렉토리의
`intent-events.jsonl`을 읽고 `.autopilot/approved-run.json`과 promoted
`conduct-plan.json` 또는 ready `tasks.jsonl` item을 쓴다.

bridge는 `--platform codex`와 `--platform claude`를 지원한다. bridge는
`--approval-evidence-json`에 approval decision(`GO`, `approve`, `approved`)과
비어 있지 않은 `source`가 없으면 run state를 쓰지 않으며, session event
metadata를 `approved-run.json` approval evidence에 보존한다.

Stop adapter에는 별도의 automatic current-session path가 있다. 프로젝트에
`.autopilot/` run state가 없지만 session ledger가 current work를 가리키고
io-trace 또는 open conduct feedback이 있으면 adapter는
`approval_evidence.decision: "AUTO"`를 쓰고 io-trace를
`autopilot_governance_signal.py`의 기존 `autopilot-observation-signal.v1`
receptor로 보낸다. Observation candidate는 diagnostic 상태로 남고
adapter-consumable action file로 promote되지 않는다.

```bash
/opt/homebrew/bin/python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex \
  --run-dir .autopilot \
  --current-work-item-id current \
  --plan-path .tmp/implementation-plans/current.md \
  --approval-evidence-json '{"decision":"GO","source":"user-confirmation"}'
```

## 요구사항

- privileged adapter를 지원하는 Ghost-ALICE core 0.1.3 이상.
- Python 3.11+.
- Ghost-ALICE core installer로 설치된 Claude Code 또는 Codex hook.

Ghost-ALICE core 0.1.3 미만에는 이 애드온을 설치하지 않는다. 오래된 core installer는 skill만 복사하고 privileged adapter를 배선하지 않을 수 있다. 이 설치는 inert 상태이므로 업그레이드 전에 제거한다.

## Compatibility Matrix

호환성 SSOT는 `compatibility-matrix.json`이다. full compatibility claim을 하기 전 반드시 이 파일을 확인한다. 이 matrix는 현재 지원 상태를 기록하는 표면이지 시간순 테스트 로그가 아니다. 날짜가 붙은 실행 산출물은 CI/test report 또는 release note에 둔다.

현재 target status:

- macOS: local unit test와 adapter subprocess simulation으로 `verified-local`.
- Claude Code: temporary hook install/remove test로 `simulated-local`.
- Linux: `not-run`.
- Windows Command Prompt: `not-run`.
- Windows PowerShell 5: `not-run`.
- Windows PowerShell 7: `not-run`.
- Codex: `verified-local` with local install status, Codex live semantic E2E, and candidate-boundary checks.

`not-run` target이 하나라도 있으면 runner evidence가 matrix에 붙기 전까지 full compatibility claim을 차단한다.
Linux와 Windows runner target은 아직 full compatibility claim을 차단한다.

## 설치

이 명령은 Ghost-ALICE core checkout에서 실행한다. 이 addon repository는
standalone root `install.sh`를 제공하지 않는다.

감지된 Claude Code/Codex 대상에 기본 설치:

```bash
bash install.sh --addon autopilot
```

Codex에만 설치:

```bash
bash install.sh --platform codex --addon autopilot
```

개발 checkout override:

```bash
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot
```

설치 상태 확인:

```bash
bash <ghost-alice>/install.sh --platform codex --status
```

## 바로 실행해 보기

프로젝트 디렉토리에서 approved run을 만든다.

```bash
mkdir -p .autopilot
cat > .autopilot/approved-run.json <<'JSON'
{
  "schema_version": "autopilot-run.v1",
  "run_id": "demo-run",
  "approved": true,
  "status": "running",
  "scope": {"summary": "Demo autopilot continuation"},
  "budget": {"remaining_steps": 2},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"}
}
JSON

cat > .autopilot/tasks.jsonl <<'JSONL'
{"id":"unit-1","status":"ready","focus_layer":"micro","depends_on":[],"prompt":"Implement the first approved demo unit.","acceptance_criteria":["the next continuation message names unit-1"],"allowed_surface":["src/..."],"completion":{"state":"not_started","verdict":null,"evidence":[],"completion_check_digest":null,"reopen_target":null},"attempt":0}
JSONL
```

다음 agent stop 이벤트 이후 adapter는 아래 형태의 continuation message를 출력한다.

```text
[autopilot]
run: demo-run
work-item: unit-1
focus-layer: micro
io-trace:
- Bash n/a apply_patch current work
governance-signal:
- candidate: candidate-<digest>
- decision: reopen_micro
- source: observation_signal
governance-evidence:
- observation_next_action:continue from latest io-trace
allowed-surface:
- src/...
acceptance-criteria:
- the next continuation message names unit-1
before-stop:
- continue from the latest io-trace when no promoted consistency decision exists.
- write .autopilot/consistency-decision.json when a completion/retry/reopen decision is resolved.
- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.
- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.
- use ask_user_meta only when neither io-trace nor work state can resolve the next action.
prompt:
Implement the first approved demo unit.
```

다음 stop 이벤트는 promoted `.autopilot/consistency-decision.json`을 소비한다. `continue_next`는 `sha256:<64-hex>` `completion_check_digest`와 `[completion-check]`, `acceptance-criteria`, 그리고 known acceptance-criteria criterion id를 참조하는 `claim-evidence-map` entry를 포함한 evidence text가 있을 때만 running item을 완료한다. `retry_same_unit`은 concrete evidence가 있을 때만 같은 item을 다시 queue에 넣는다. `reopen_micro`, `reopen_meso`, `reopen_macro`는 같은 item을 open 상태로 유지하고 다음 continuation message에 요청된 focus layer를 표면화한다. running item에 decision file이 없으면 adapter는 silent no-op 대신 `pending-decision: missing`으로 같은 item을 재개하고, repeated missing decision은 io-trace와 work state 어느 쪽으로도 next action을 resolved할 수 없을 때만 `ask_user_meta`로 escalate한다.

## 일시정지, 재개, 중지

일시정지:

```bash
touch .autopilot/OFF
```

재개:

```bash
rm .autopilot/OFF
```

중지는 아래 중 하나로 수행한다.

- `approved-run.json`의 `status`를 `stopped`로 설정
- `approved`를 false로 설정
- `budget.remaining_steps`를 0으로 설정
- `approved-run.json` 제거

## 제거

이 애드온만 제거:

```bash
bash <ghost-alice>/install.sh \
  --platform codex \
  --uninstall --addon autopilot-mode
```

Claude Code는 `--platform claude`를 사용한다. 제거는 `--addon-source`가 아니라 설치된 addon id와 sidecar 기준으로 수행된다.

Ghost-ALICE 전체 제거는 core full-uninstall 경로를 사용한다.

```bash
bash <ghost-alice>/install.sh --uninstall
```

## 제한 및 신뢰 메모

- 애드온 설치는 런타임 활성화가 아니다.
- adapter는 인자를 받지 않는다.
- adapter는 프로젝트 로컬 `.autopilot/` run-state file만 변경하고 continuation payload를 출력한다.
- continuation payload는 실행 중인 agent가 멈추기 전에 promoted `.autopilot/consistency-decision.json`을 남기도록 `before-stop` contract를 포함한다.
- `consistency-decision.candidate.json`, `conduct-plan.candidate.json` 같은 candidate file은 adapter-consumable이 아니다.
- `conduct-plan.json`은 `schema_version: "autopilot-conduct-plan.v2"`를 사용하고 `promotion_state: "approved"`, `approval_evidence`, source candidate id, evidence digest를 포함해야 한다.
- conduct plan proposal은 `proposal_status: "proposed"`, `approval_required: true`, `task_template`을 `ready`로 복사하는 approval transition을 유지해야 한다.
- 가져온 proposal은 `observer_agent_required`와 `observer_contract`를 보존하고, continuation message는 read-only observer requirement를 표면화한다.
- 이미 존재하는 task id는 건너뛰므로 conduct plan import는 idempotent하다.
- tool denial, installer policy, privileged adapter allowlist, hook marker, runner namespace, hook install/remove 동작은 Ghost-ALICE core가 소유한다.
- 이 애드온 패키지는 skill content와 adapter implementation을 소유한다.

## 저장소 구조

```text
addons-manifest.json
compatibility-matrix.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_messages.py
  skill/adapters/autopilot_mode.py
  skill/adapters/autopilot_state.py
  skill/adapters/autopilot_work_items.py
  skill/scripts/autopilot_governance_signal.py
  skill/scripts/autopilot_session_bridge.py
  skill/scripts/autopilot_session_material.py
tests/
scripts/autopilot_session_bridge.py
```

## 라이선스

Apache-2.0. [LICENSE](./LICENSE), [NOTICE](./NOTICE) 참고.
