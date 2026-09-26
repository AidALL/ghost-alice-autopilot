# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

승인하신 작업을 이어서 실행하고, 완료 조건을 확인하는 공식 Ghost-ALICE 애드온입니다.

Language: [English](./README.md) | Korean

`autopilot-mode`는 Ghost-ALICE가 승인된 작업을 이어서 수행하도록 돕습니다. 에이전트의 한 차례 응답이 끝나면 현재 요청, 승인 범위, 완료 기록을 확인하고 다음 작업이나 아직 끝나지 않은 작업을 선택합니다. 사용자의 승인과 중지 상태를 따르며, 완료 조건을 확인한 뒤 다음 단계로 넘어가도록 구성되어 있습니다.

이 저장소는 Claude Code·Codex에 연결하는 애드온을 제공합니다. 사용하시려면 Ghost-ALICE core와 해당 실행 환경이 먼저 필요합니다. 아래에서 설치 방법, 실행 예시, 일시정지·중지 방법을 확인하실 수 있습니다.

내부적으로는 에이전트의 stop 이벤트 이후 프로젝트의 `.autopilot/` 상태를 읽습니다. `ready` 또는 `reopened` 작업을 선택하고, 현재 io-trace 자료가 있으면 미완료 `running` 작업을 재개하도록 후속 실행 메시지(continuation message)를 출력합니다.

현재 릴리스는 `0.3.0`이며 Ghost-ALICE core `0.3.0`과 함께 사용하시기를 권장합니다. [릴리스 노트](./docs/ko/release/2026-09-26-release-notes.md), [GitHub 릴리스](https://github.com/AidALL/ghost-alice-autopilot/releases/tag/v0.3.0), [Ghost-ALICE 홈페이지](https://aidall.github.io/ghost-alice/)에서 변경 내용을 확인하실 수 있습니다. 두 프로젝트는 Apache-2.0 라이선스의 오픈소스로 유지됩니다.

## 이 애드온이 하는 일

- `autopilot-mode` skill을 설치합니다.
- Ghost-ALICE installer를 통해 core-owned `[adapter:autopilot-mode] continue` hook을 등록합니다.
- 프로젝트 로컬 `.autopilot/` 실행 상태를 읽으며, Claude에서는 hook 실행 시점의 `cwd`보다 안정적인 `CLAUDE_PROJECT_DIR`을 우선합니다.
- 명시적 승인 후 session-intent ledger file에서 `.autopilot/`을 부트스트랩하는 `skill/scripts/autopilot_session_bridge.py`와 repository wrapper `scripts/autopilot_session_bridge.py`를 제공합니다.
- Stop hook bootstrap은 session lineage에 연결됩니다. hook이 명시적 session id를 제공하면 다른 session의 오래된 `current-session.json` pointer로 fallback하지 않습니다.
- Stop adapter가 별도 receptor를 만들지 않고, session intent에 admitted 상태의 미충족 acceptance criteria가 기록되어 있거나 open conduct feedback이 승인된 conduct plan을 제공할 때 current-session `.autopilot/` state를 materialize합니다. io-trace 자료만으로는 run을 부트스트랩하지 않으며 `autopilot-observation-signal.v1` receptor를 통한 observation으로만 처리됩니다.
- `autopilot_governance_signal.py`로 evidence-backed governance candidate와 promotion을 만듭니다.
- 승인된 `conduct-plan.json` proposal queue를 durable `tasks.jsonl` work item으로 가져옵니다.
- no-op payload 또는 다음 work-item message를 출력합니다.
- adapter event를 `.autopilot/events.jsonl`에 기록합니다.

이 애드온은 현재 세션 밖의 작업을 만들지 않습니다. 사용자 의도 분석, 작업 라우팅, 명시적 GO 결정과 현재 세션의 실행 자료를 바탕으로 승인된 실행 상태를 구성합니다. 실행을 시작하거나 이어가는 구체적인 조건은 아래에 설명합니다.

## 동작 방식

런타임 흐름:

1. Ghost-ALICE core installer가 이 애드온을 설치하고 privileged adapter hook을 배선합니다.
2. 프로젝트는 사용자 승인 후 `.autopilot/approved-run.json`과 `.autopilot/tasks.jsonl`을 만듭니다. conduct-feedback 실행은 승인된 `.autopilot/conduct-plan.json`을 대신 제공할 수 있습니다. package bridge `skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper `scripts/autopilot_session_bridge.py`는 caller가 명시적 approval evidence를 제공할 때 `current-session.json`, `intent-state.json`, `intent-events.jsonl`에서 이 run state를 만들 수 있습니다. Stop adapter는 session intent에 admitted 상태의 미충족 acceptance criteria가 기록되어 있거나 승인된 conduct plan이 있을 때 current session을 materialize할 수도 있습니다. io-trace material만으로는 bootstrap approval이 되지 않으며 observation/resume material로만 쓰입니다.
3. 에이전트가 멈추면 adapter가 `.autopilot/`을 읽습니다.
4. governance signal은 먼저 `consistency-decision.candidate.json` 또는 `conduct-plan.candidate.json`을 씁니다. 이 candidate file은 adapter-consumable이 아닙니다.
5. promotion만 adapter-consumable `consistency-decision.json` 또는 승인된 `conduct-plan.json`을 만듭니다.
6. `conduct-plan.json`이 있으면 adapter는 ready task 확인 전에 새 proposed queue item을 `tasks.jsonl`로 가져옵니다.
7. 실행이 approved, running, 예산 내 상태이고 ready 또는 reopened task가 있으면 adapter가 해당 task를 `running`으로 표시합니다.
8. running task에 promoted decision이 없지만 current io-trace가 있으면 adapter는 io-trace를 `autopilot-observation-signal.v1`로 넣고 같은 task를 재개합니다.
9. adapter가 다음 work item과 decision이 resolved되었을 때 `.autopilot/consistency-decision.json` 작성 또는 promotion을 요구하는 `before-stop` 지시가 담긴 continuation payload를 출력합니다.
10. 실행이 승인되지 않았거나, pause/stop 상태이거나, 예산이 없거나, runnable item 또는 runtime material이 없으면 no-op payload를 반환합니다.

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

`GHOST_ALICE_AUTOPILOT_RUN_DIR`은 오류를 숨기지 않는 최우선 run-directory override이고, `GHOST_ALICE_AUTOPILOT_CWD`는 그다음 project-root override입니다. Project-root override는 절대경로여야 하며, 상대값은 process directory 기준으로 해석하지 않고 blocking adapter reason으로 표면화합니다. 두 override가 없으면 Claude hook은 `CLAUDE_PROJECT_DIR`과 hook input의 `cwd`에서 처음 발견한 비어 있지 않은 절대경로를 선택하고, Codex는 상속된 `CLAUDE_PROJECT_DIR`을 무시하고 hook input의 `cwd`를 선택합니다. 두 platform 모두 그다음 절대 adapter process directory를 fallback으로 사용합니다. 상대 파생 후보는 건너뛰되 경로 선택 뒤 접근 오류가 발생했다고 낮은 우선순위 후보를 다시 시도하지 않습니다. 파생된 `<project>/.autopilot` directory 생성 또는 lock 획득 중 발생한 `PermissionError`만 빈 no-op payload로 바꾸며, 이후 state processing 예외와 명시적 run-directory 오류는 그대로 표면화합니다.

## Governance Candidate와 Promotion

`addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py`는 session intent, conduct feedback, routing-surface correction, completion validation failure를 evidence-backed candidate file로 변환합니다. candidate file은 진단 출력일 뿐입니다.

- `consistency-decision.candidate.json`은 `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용합니다.
- `conduct-plan.candidate.json`은 `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용합니다.
- candidate schema가 adapter-consumable 경로에 잘못 놓여도 adapter는 이를 거부합니다.

promotion은 adapter-consumable file을 만드는 경계입니다. `promote-decision`은 `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `state_hash`, `decision_key`, `loop_key`를 포함한 `consistency-decision.json`을 씁니다. `promotion_evidence.decision`은 `go`, `approve`, `approved`, `promote`, `promoted`, `direct`를 허용합니다. `direct`는 candidate가 없는 current-turn before-stop resolution에만 사용합니다. 모든 promoted decision에서 `evidence`는 JSON array of strings이어야 합니다. `verdict`, `completion_check_digest`, `text`를 `evidence` 안에 중첩하지 않습니다. `promote-conduct-plan`은 `promotion_state: "approved"`, approval evidence, source candidate id, evidence digest를 포함한 승인 `conduct-plan.json`을 씁니다.

State-aware promotion은 어댑터가 사용할 action 파일을 쓰기 전에 `--run-dir` 또는 candidate 파일의 상위 실행 디렉터리에서 대상 작업의 상태를 확인합니다. `continue_next`는 `running`, `ready`, `reopened` 상태를 허용하며, 나머지 결정은 `running` 상태를 요구합니다. 대상이 없거나 상태가 호환되지 않으면 candidate를 진단 자료로 남기고 `consistency-decision.json`을 생성하지 않은 채 종료합니다. 같은 실행 상태의 재시도 횟수와 이전 decision/state loop key도 확인하며, 재시도 한도에 도달했거나 반복 루프가 확인되면 `ask_user_meta`로 사용자 판단을 요청합니다.

## Session-Intent Bridge

Stop adapter는 `GHOST_ALICE_PLATFORM=agent-runtime`, 명시적 `GHOST_ALICE_SESSION_ID`, 절대경로인 `GHOST_ALICE_SESSION_INTENT_ROOT`를 전달하는 host도 처리합니다. `<root>/agent-runtime/<session-id>/intent-state.json`만 읽으며 다른 플랫폼 원장이나 공유 current-session pointer로 대체하지 않습니다. 원장은 `session-intent-ledger.v1` 스키마와 선택한 platform·session identity가 일치해야 합니다. 문맥이 없거나 충돌하면 pending receipt·plan 적용 전에 중단합니다. 유효한 receipt는 한 번만 소비하며, 승인된 목표 안에서의 구체화는 기존 승인을 유지합니다. 알 수 없는 명시적 platform은 Codex·Claude로 fallback하지 않습니다. 모델·도구 실행·이벤트 전달은 host의 책임이며 이 어댑터 계약이 해당 host 기능을 설치하지는 않습니다.

설치만으로 `.autopilot/`은 생성되지 않습니다. 현재 Ghost-ALICE session ledger에서 approved run을 활성화하려면 package bridge `skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper `scripts/autopilot_session_bridge.py`를 사용합니다. bridge는 `.tmp/session-intent/<platform>/current-session.json`, 그 pointer가 가리키는 `intent-state.json`, 같은 디렉토리의 `intent-events.jsonl`을 읽고 `.autopilot/approved-run.json`과 promoted `conduct-plan.json` 또는 ready `tasks.jsonl` item을 씁니다.

bridge는 `--platform codex`와 `--platform claude`를 지원합니다. bridge는 `--approval-evidence-json`에 approval decision(`GO`, `approve`, `approved`)과 비어 있지 않은 `source`가 없으면 run state를 쓰지 않으며, session event metadata를 `approved-run.json` approval evidence에 보존합니다.

Stop adapter에는 별도의 automatic current-session path가 있습니다. 프로젝트에 `.autopilot/` run state가 없고 session ledger에 admitted 상태이면서 아직 met되지 않은 acceptance criteria가 기록되어 있으면 adapter는 `approval_evidence.decision: "AUTO"`(`source: "admitted-unmet-criterion"`)로 run state를 bootstrap합니다. io-trace 존재만으로는 run을 bootstrap하지 않으며, io-trace는 `autopilot_governance_signal.py`의 기존 `autopilot-observation-signal.v1` receptor로 보냅니다. Observation candidate는 diagnostic 상태로 남고 adapter-consumable action file로 promote되지 않습니다.

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

- privileged adapter와 schema-preserving hook rendering을 지원하는 Ghost-ALICE core 0.2.2 이상이 필요합니다.
- Python 3.11 이상이 필요합니다.
- Ghost-ALICE core 설치기로 설치한 Claude Code 또는 Codex 훅이 필요합니다.

Ghost-ALICE core 0.2.2 미만에는 이 애드온을 설치하지 마세요. 이전 설치기는 skill만 복사하고 privileged adapter, runtime-core audit, ledger met-flip path, schema-preserving hook renderer를 연결하지 못할 수 있습니다. 이 경우 설치되어 있어도 활성화되지 않는 inert 상태이거나 설치가 불완전할 수 있으므로, 기존 애드온 설치를 제거한 뒤 core를 업그레이드해 주세요.

설치 호환성 하한은 core `0.2.2`로 유지됩니다. 현재 의도 snapshot과 세션에 연결된 hook 갱신을 함께 사용하시려면 core `0.3.0` / 애드온 `0.3.0` 조합을 권장합니다. 제품 버전을 맞추어도 schema 버전이 바뀌거나 독립적인 모델 실행 환경이 추가되지는 않습니다.

## Compatibility Matrix

아래 매트릭스는 기존에 확인한 지원 상태를 나타냅니다. Claude·Codex의 live 검증 항목에는 이전 릴리스의 근거가 포함되어 있으며, 모든 항목을 `0.3.0`에서 다시 수행했다는 뜻은 아닙니다. [현재 릴리스 노트](./docs/ko/release/2026-09-26-release-notes.md#검증과-한계)에서 이번 설치본 재생·회귀 검사와 새 모델 추론 검증의 범위를 구분해 안내합니다.

호환성 SSOT는 `compatibility-matrix.json`입니다. full compatibility claim을 하기 전 반드시 이 파일을 확인합니다. 이 matrix는 현재 지원 상태를 기록하는 표면이지 시간순 테스트 로그가 아닙니다. 날짜가 붙은 실행 산출물은 CI/test report 또는 release note에 둡니다.

현재 플랫폼별 검증 상태는 다음과 같습니다.

- macOS: 로컬 단위 시험과 어댑터 subprocess 시뮬레이션으로 확인한 `verified-local` 상태입니다.
- Claude Code: `verified-local` 상태입니다. 로컬 설치 상태, 인증된 Claude의 실제 의미 해석 E2E 시험, 시험 목적을 숨긴 core blind-controller 사례 5건을 확인했습니다.
- Linux: 아직 시험하지 않은 `not-run` 상태입니다.
- Windows Command Prompt: 아직 시험하지 않은 `not-run` 상태입니다.
- Windows PowerShell 5: 아직 시험하지 않은 `not-run` 상태입니다.
- Windows PowerShell 7: 아직 시험하지 않은 `not-run` 상태입니다.
- Codex: `verified-local` 상태입니다. 로컬 설치 상태, 실제 의미 해석 E2E 시험(Codex live semantic E2E), candidate 경계 검사, 시험 목적을 숨긴 core blind-controller 사례 5건을 확인했습니다.

`not-run` target이 하나라도 있으면 runner evidence가 matrix에 붙기 전까지 full compatibility claim을 차단합니다. Linux와 Windows runner target은 아직 full compatibility claim을 차단합니다.

## 설치

다음 명령은 내려받은 Ghost-ALICE core 저장소 디렉터리에서 실행해 주세요. 이 애드온 저장소에는 독립 실행용 루트 `install.sh`가 없습니다.

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

아래는 실행 상태와 작업 한 개를 직접 구성하는 예시입니다. 사용하실 프로젝트 디렉터리에서 승인 범위에 맞는 approved run을 만들어 주세요.

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

다음 agent stop 이벤트 이후 adapter는 아래 형태의 continuation message를 출력합니다.

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
- promote a candidate with scripts/autopilot_governance_signal.py promote-decision when a candidate exists.
- otherwise write .autopilot/consistency-decision.json only with the full promoted schema when a completion/retry/reopen decision is resolved.
- promoted schema requires schema_version, decision_id, work_item_id, decision, promotion_state: promoted, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, decision_key, state_hash, loop_key, and evidence.
- promotion_evidence.decision must be one of go, approve, approved, promote, promoted, or direct; use direct only for a current-turn before-stop resolution without a candidate.
- evidence must be a JSON array of strings; do not nest verdict, completion_check_digest, or text inside evidence.
- for continue_next, put verdict and completion_check_digest at top level and put the full [completion-check] block in evidence strings.
- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.
- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.
- use ask_user_meta only when neither io-trace nor work state can resolve the next action.
prompt:
Implement the first approved demo unit.
```

다음 stop 이벤트는 promoted `.autopilot/consistency-decision.json`을 소비합니다. 직접 completion decision을 쓸 때도 `before-stop` block에 명시된 full promoted action schema를 포함해야 합니다. 일부 필드만 손으로 쓴 decision은 거부되고 `.autopilot/consistency-decision.rejected.json`으로 보존됩니다. `continue_next`는 `sha256:<64-hex>` `completion_check_digest`와 `[completion-check]`, `acceptance-criteria`, 그리고 known acceptance-criteria criterion id를 참조하는 `claim-evidence-map` entry를 포함한 evidence text가 있을 때만 running item을 완료합니다. `retry_same_unit`은 concrete evidence가 있을 때만 같은 item을 다시 queue에 넣습니다. `reopen_micro`, `reopen_meso`, `reopen_macro`는 같은 item을 open 상태로 유지하고 다음 continuation message에 요청된 focus layer를 표면화합니다. running item에 decision file이 없으면 adapter는 silent no-op 대신 `pending-decision: missing`으로 같은 item을 재개하고, repeated missing decision은 io-trace와 work state 어느 쪽으로도 next action을 resolved할 수 없을 때만 `ask_user_meta`로 escalate합니다.

## 일시정지, 재개, 중지

일시정지:

```bash
touch .autopilot/OFF
```

재개:

```bash
rm .autopilot/OFF
```

실행을 중지하시려면 다음 방법 중 하나를 사용해 주세요.

- `approved-run.json`의 `status`를 `stopped`로 설정합니다.
- `approved`를 false로 설정합니다.
- `budget.remaining_steps`를 0으로 설정합니다.
- `approved-run.json`을 제거합니다.

## 제거

이 애드온만 제거:

```bash
bash <ghost-alice>/install.sh \
  --platform codex \
  --uninstall --addon autopilot-mode
```

Claude Code에서 제거하시려면 `--platform claude`를 사용해 주세요. 제거는 `--addon-source`가 아니라 설치된 addon id와 sidecar를 기준으로 수행됩니다.

Ghost-ALICE 전체 제거는 core full-uninstall 경로를 사용합니다.

```bash
bash <ghost-alice>/install.sh --uninstall
```

## 제한 및 신뢰 메모

- 애드온 설치는 런타임 활성화가 아닙니다.
- adapter는 인자를 받지 않습니다.
- adapter는 프로젝트 로컬 `.autopilot/` run-state file만 변경하고 continuation payload를 출력합니다.
- continuation payload는 실행 중인 agent가 멈추기 전에 promoted `.autopilot/consistency-decision.json`을 남기도록 `before-stop` contract를 포함합니다.
- `consistency-decision.candidate.json`, `conduct-plan.candidate.json` 같은 candidate file은 adapter-consumable이 아닙니다.
- `conduct-plan.json`은 `schema_version: "autopilot-conduct-plan.v2"`를 사용하고 `promotion_state: "approved"`, `approval_evidence`, source candidate id, evidence digest를 포함해야 합니다.
- conduct plan proposal은 `proposal_status: "proposed"`, `approval_required: true`, `task_template`을 `ready`로 복사하는 approval transition을 유지해야 합니다.
- 가져온 proposal은 `observer_agent_required`와 `observer_contract`를 보존하고, continuation message는 read-only observer requirement를 표면화합니다.
- 이미 존재하는 task id는 건너뛰므로 같은 conduct plan을 다시 가져와도 작업이 중복 생성되지 않습니다.
- tool denial, installer policy, privileged adapter allowlist, hook marker, runner namespace, hook install/remove 동작은 Ghost-ALICE core가 소유합니다.
- 이 애드온 패키지는 skill content와 adapter implementation을 소유합니다.

## 저장소 구조

```text
addons-manifest.json
compatibility-matrix.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_lineage.py
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

Apache-2.0 라이선스를 따릅니다. 자세한 내용은 [LICENSE](./LICENSE)와 [NOTICE](./NOTICE)를 확인해 주세요.
