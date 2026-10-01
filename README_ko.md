# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

승인하신 작업을 이어서 실행하고, 완료 조건을 확인하는 공식 Ghost-ALICE 애드온입니다.

Language: [English](./README.md) | Korean

`autopilot-mode`는 Ghost-ALICE가 승인된 작업을 이어서 수행하도록 돕습니다. 에이전트의 한 차례 응답이 끝나면 현재 요청, 승인 범위, 완료 기록을 확인하고 다음 작업이나 아직 끝나지 않은 작업을 선택합니다. 사용자의 승인과 중지 상태를 따르며, 완료 조건을 확인한 뒤 다음 단계로 넘어가도록 구성되어 있습니다.

이 저장소는 Claude Code·Codex에 연결하는 애드온을 제공합니다. 사용하시려면 Ghost-ALICE core와 해당 실행 환경이 먼저 필요합니다. 아래에서 설치 방법, 실행 예시, 일시정지·중지 방법을 확인하실 수 있습니다.

내부적으로는 에이전트의 stop 이벤트 이후 프로젝트의 `.autopilot/` 상태를 읽습니다. `ready` 또는 `reopened` 작업을 선택하고, 현재 io-trace 자료가 있으면 미완료 `running` 작업을 재개하도록 후속 실행 메시지(continuation message)를 출력합니다.

현재 소스 버전은 `0.4.1`이며 Ghost-ALICE core `0.4.1`과 함께 사용하시기를 권장합니다. [릴리스 노트](./docs/ko/release/2026-10-01-release-notes.md), [GitHub 릴리스](https://github.com/AidALL/ghost-alice-autopilot/releases), [Ghost-ALICE 홈페이지](https://aidall.github.io/ghost-alice/)에서 변경 내용을 확인하실 수 있습니다. 두 프로젝트는 Apache-2.0 라이선스의 오픈소스로 유지됩니다.

## 빠른 설치

복제한 Ghost-ALICE core 저장소 폴더에서 플랫폼 자동 감지로 Core와 Autopilot을 함께 설치해 주세요.

```bash
bash install.sh --addon autopilot
```

저장소 복제, Windows 기본 명령과 플랫폼별 옵션은 [상세 설치 안내](https://github.com/AidALL/ghost-alice/blob/main/docs/ko/getting-started/installation.md)를 확인해 주세요.

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
- adapter event를 `<selected-run-dir>/events.jsonl`에 기록합니다.

이 애드온은 현재 세션 밖의 작업을 만들지 않습니다. 사용자 의도 분석, 작업 라우팅, 명시적 GO 결정과 현재 세션의 실행 자료를 바탕으로 승인된 실행 상태를 구성합니다. 실행을 시작하거나 이어가는 구체적인 조건은 아래에 설명합니다.

## 동작 방식

런타임 흐름:

1. Ghost-ALICE core installer가 이 애드온을 설치하고 privileged adapter hook을 배선합니다.
2. 프로젝트는 사용자 승인 후 `<selected-run-dir>/approved-run.json`과 `<selected-run-dir>/tasks.jsonl`을 만듭니다. conduct-feedback 실행은 승인된 `<selected-run-dir>/conduct-plan.json`을 대신 제공할 수 있습니다. package bridge `skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper `scripts/autopilot_session_bridge.py`는 caller가 명시적 approval evidence를 제공할 때 `current-session.json`, `intent-state.json`, `intent-events.jsonl`에서 이 run state를 만들 수 있습니다. Stop adapter는 session intent에 admitted 상태의 미충족 acceptance criteria가 기록되어 있거나 승인된 conduct plan이 있을 때 current session을 materialize할 수도 있습니다. io-trace material만으로는 bootstrap approval이 되지 않으며 observation/resume material로만 쓰입니다.
3. 에이전트가 멈추면 adapter가 `.autopilot/`을 읽습니다.
4. governance signal은 먼저 `consistency-decision.candidate.json` 또는 `conduct-plan.candidate.json`을 씁니다. 이 candidate file은 adapter-consumable이 아닙니다.
5. promotion만 adapter-consumable `consistency-decision.json` 또는 승인된 `conduct-plan.json`을 만듭니다.
6. `conduct-plan.json`이 있으면 adapter는 ready task 확인 전에 새 proposed queue item을 `tasks.jsonl`로 가져옵니다.
7. 실행이 approved, running, 예산 내 상태이고 ready 또는 reopened task가 있으면 adapter가 해당 task를 `running`으로 표시합니다.
8. running task에 promoted decision이 없지만 current io-trace가 있으면 adapter는 io-trace를 `autopilot-observation-signal.v1`로 넣고 같은 task를 재개합니다.
9. adapter가 다음 work item과 decision이 resolved되었을 때 `<selected-run-dir>/consistency-decision.json` 작성 또는 promotion을 요구하는 `before-stop` 지시가 담긴 continuation payload를 출력합니다.
10. 실행이 승인되지 않았거나, pause/stop 상태이거나, 예산이 없거나, runnable item 또는 runtime material이 없으면 no-op payload를 반환합니다.

Pretool, 완료 준비·등록, Stop은 같은 실행 경로를 선택합니다. 기존 프로젝트 루트 실행은 플랫폼·세션과 확인된 원장 루트가 현재 세션과 일치할 때만 재사용하며, 다른 세션의 실행은 보존합니다. 세션 ID가 없는 수동 실행은 기존 경로를 유지합니다. 작업 파일에는 확인 결과의 `automatic_target.run_dir` 또는 완료 영수증의 `run_dir`을 사용합니다. 다른 세션을 가리키는 명시적 경로는 `ownership-conflict`로 알리며 실행 불가능한 준비 명령을 제공하지 않습니다. `<project>/.autopilot/OFF`는 모든 기본 세션 실행을, `<selected-run-dir>/OFF`는 해당 실행만 일시정지합니다. 완료 준비·등록에도 적용됩니다.

세션이 식별된 경우의 기본 run directory:

```text
<project>/.autopilot/sessions/<platform>/<session-id>/
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

`GHOST_ALICE_AUTOPILOT_RUN_DIR`은 오류를 숨기지 않는 최우선 run-directory override이고, `GHOST_ALICE_AUTOPILOT_CWD`는 그다음 project-root override입니다. Project-root override는 절대경로여야 하며, 상대값은 process directory 기준으로 해석하지 않고 blocking adapter reason으로 표면화합니다. 두 override가 없으면 Claude hook은 `CLAUDE_PROJECT_DIR`과 hook input의 `cwd`에서 처음 발견한 비어 있지 않은 절대경로를 선택하고, Codex는 상속된 `CLAUDE_PROJECT_DIR`을 무시하고 hook input의 `cwd`를 선택합니다. 두 platform 모두 그다음 절대 adapter process directory를 fallback으로 사용합니다. 상대 파생 후보는 건너뛰되 경로 선택 뒤 접근 오류가 발생했다고 낮은 우선순위 후보를 다시 시도하지 않습니다. 파생된 `<project>/.autopilot` directory 생성 또는 lock 획득 중 발생한 권한 오류 또는 읽기 전용 파일시스템 오류만 빈 no-op payload로 바꾸며, 이후 state processing 예외와 명시적 run-directory 오류는 그대로 표면화합니다.

## Governance Candidate와 Promotion

`addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py`는 session intent, conduct feedback, routing-surface correction, completion validation failure를 evidence-backed candidate file로 변환합니다. candidate file은 진단 출력일 뿐입니다.

- `consistency-decision.candidate.json`은 `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용합니다.
- `conduct-plan.candidate.json`은 `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, `action_file_allowed: false`를 사용합니다.
- candidate schema가 adapter-consumable 경로에 잘못 놓여도 adapter는 이를 거부합니다.

promotion은 adapter-consumable file을 만드는 경계입니다. `promote-decision`은 `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `state_hash`, `decision_key`, `loop_key`를 포함한 `consistency-decision.json`을 씁니다. `promotion_evidence.decision`은 `go`, `approve`, `approved`, `promote`, `promoted`, `direct`를 허용합니다. `direct`는 candidate가 없는 current-turn before-stop resolution에만 사용합니다. 모든 promoted decision에서 `evidence`는 JSON array of strings이어야 합니다. `verdict`, `completion_check_digest`, `text`를 `evidence` 안에 중첩하지 않습니다. `promote-conduct-plan`은 `promotion_state: "approved"`, approval evidence, source candidate id, evidence digest를 포함한 승인 `conduct-plan.json`을 씁니다.

State-aware promotion은 어댑터가 사용할 action 파일을 쓰기 전에 `--run-dir` 또는 candidate 파일의 상위 실행 디렉터리에서 대상 작업의 상태를 확인합니다. `continue_next`는 `running`, `ready`, `reopened` 상태를 허용하며, 나머지 결정은 `running` 상태를 요구합니다. 대상이 없거나 상태가 호환되지 않으면 candidate를 진단 자료로 남기고 `consistency-decision.json`을 생성하지 않은 채 종료합니다. 같은 실행 상태의 재시도 횟수와 이전 decision/state loop key도 확인하며, 재시도 한도에 도달했거나 반복 루프가 확인되면 `ask_user_meta`로 사용자 판단을 요청합니다.

## Session-Intent Bridge

Stop adapter는 `GHOST_ALICE_PLATFORM=agent-runtime`, 명시적 `GHOST_ALICE_SESSION_ID`, 절대경로인 `GHOST_ALICE_SESSION_INTENT_ROOT`를 전달하는 host도 처리합니다. Core 원장 API로 정확한 `agent-runtime` 세션만 선택하며 다른 플랫폼 원장이나 공유 current-session pointer로 대체하지 않습니다. 원장은 `session-intent-ledger.v1` 스키마와 선택한 platform·session identity가 일치해야 합니다. 문맥이 없거나 충돌하면 pending receipt·plan 적용 전에 중단합니다. 유효한 receipt는 한 번만 소비하며, 승인된 목표 안에서의 구체화는 기존 승인을 유지합니다. 알 수 없는 명시적 platform은 Codex·Claude로 fallback하지 않습니다. 모델·도구 실행·이벤트 전달은 host의 책임이며 이 어댑터 계약이 해당 host 기능을 설치하지는 않습니다.

설치만으로 `.autopilot/`은 생성되지 않습니다. 현재 Ghost-ALICE session ledger에서 approved run을 활성화하려면 package bridge `skill/scripts/autopilot_session_bridge.py` 또는 repository wrapper `scripts/autopilot_session_bridge.py`를 사용합니다. bridge는 Core SQLite 원장 API로 정확한 현재 세션의 상태를 읽습니다. `.tmp/session-intent/ghost-state.sqlite3`가 런타임 기준이며 `current-session.json`, `intent-state.json`, `intent-events.jsonl`은 호환성·내보내기 또는 검증된 기존 자료 가져오기에 사용합니다. 승인 상태는 확인된 선택 실행 디렉터리에 기록하며 프로젝트 수준의 `.autopilot/`을 추측하지 않습니다.

승인 전에 상태를 생성하지 않고 정확한 세션과 대상을 확인해 주세요.

```bash
python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --run-dir .autopilot --check
```

결과의 최신 입력 이벤트와 `automatic_target`은 Stop adapter와 같은 실행 선택 코드를 사용합니다. 사용자 지정 `--run-dir`이 호스트 Stop 대상을 바꾸지는 않습니다. `matches_requested_run: false`이면 현재 환경이 다른 디렉터리를 선택한다는 뜻이며, 엄격한 `GHOST_ALICE_AUTOPILOT_RUN_DIR` override를 보존해 주세요. 이 진단은 현재 프로세스 환경과 작업 디렉터리를 설명하며 미래 훅의 환경을 보장하지 않습니다. 승인은 확인에 사용한 영수증의 `--input-event-id <checked-event-id>`를 요구합니다. 입력이 바뀌면 이전 승인을 거부하며 새 입력으로 자동 연결하지 않습니다.

승인 등록은 기록 전에 원장 스키마, platform, session identity와 최신 input identity를 검증합니다. 다른 세션의 실행이나 세션에 연결되지 않은 기존 실행을 교체하지 않습니다. Adapter와 같은 잠금을 사용하여 관측한 입력과 의도 상태를 다시 확인하며, 동시에 변경되면 새 확인이 필요합니다. 이 검사는 기록된 출처를 검증하며 유사한 주제에서 승인을 추론하지 않습니다.

세션에 연결된 완료는 캡처된 입력 영수증과 기준 정의를 받는 Core writer를 요구합니다. Adapter는 작업을 `completed`로 바꾸기 전에 Core 완료 기록을 남깁니다. Core 영수증이 거부되거나 사용할 수 없으면 작업을 미완료로 유지하고 거부된 결정을 보존합니다. 기준 스냅샷이 없는 이전 승인은 현재 입력의 재승인이 필요하며, 세션에 연결되지 않은 독립적인 기존 실행은 별도의 task-only 계약을 유지합니다.

세션 실행의 `approval_generation`은 승인된 입력, 세션, 기준 정의와 범위의 digest입니다. 근거를 만들 때 이 값을 캡처하고 모든 decision·conduct-plan candidate와 action에 보존해 주세요. 두 candidate 명령은 `--approval-generation <captured-value>`를 받으며 promotion은 기존 값을 보존합니다. 이전 근거에 현재 세대를 덧붙이지 않습니다. 값이 없거나 오래되면 작업과 Core 기준을 바꾸기 전에 거부합니다. 새 입력, 기준 정의나 범위 변경은 재승인과 새 근거를 요구합니다. 이전 실행과 미처리 자료는 `.approval-history/`에 보존하며 이전 세대의 늦은 자료는 유효해지지 않습니다. 같은 입력과 계약으로 bridge를 반복하면 진행 상태, 미처리 근거, 남은 예산과 `OFF`를 유지합니다. 이 digest는 출처 검사이며 모델 근거의 참됨을 증명하지는 않습니다.

bridge는 `--platform codex`와 `--platform claude`를 지원합니다. bridge는 `--approval-evidence-json`에 approval decision(`GO`, `approve`, `approved`)과 비어 있지 않은 `source`가 없으면 run state를 쓰지 않으며, session event metadata를 `approved-run.json` approval evidence에 보존합니다.

Stop adapter에는 별도의 automatic current-session path가 있습니다. 선택한 현재 세션 실행에 approved state가 없고 session ledger에 admitted 상태이면서 아직 met되지 않은 acceptance criteria가 기록되어 있으면 adapter는 `approval_evidence.decision: "AUTO"`(`source: "admitted-unmet-criterion"`)로 run state를 bootstrap합니다. io-trace 존재만으로는 run을 bootstrap하지 않으며, io-trace는 `autopilot_governance_signal.py`의 기존 `autopilot-observation-signal.v1` receptor로 보냅니다. Observation candidate는 diagnostic 상태로 남고 adapter-consumable action file로 promote되지 않습니다.

현재 세션을 확인하고 승인 항목을 등록하는 절차는 [바로 실행해 보기](#바로-실행해-보기)를 확인해 주세요.

## 요구사항

- 공유 SQLite 런타임, privileged adapter와 schema-preserving hook을 지원하는 Ghost-ALICE Core `0.4.0` 이상이 필요합니다.
- Python 3.11 이상이 필요합니다.
- Core 설치기로 설치한 Claude Code 또는 Codex 훅이 필요합니다.

권장 제품 조합은 Core `0.4.1` / Autopilot `0.4.1`입니다. 스키마 버전은 독립적으로 유지되며, 모델과 도구 실행은 호스트 실행 환경이 제공합니다.

## Compatibility Matrix

아래 매트릭스는 기존에 확인한 지원 상태를 나타냅니다. Claude·Codex의 live 검증 항목에는 이전 릴리스의 근거가 포함되어 있으며, 모든 항목을 `0.4.1`에서 다시 수행했다는 뜻은 아닙니다. [현재 릴리스 노트](./docs/ko/release/2026-10-01-release-notes.md#검증-범위)에서 이번 설치본 재생·회귀 검사와 새 모델 추론 검증의 범위를 구분해 안내합니다.

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

실제로 승인하신 목표, 계획, 허용 범위와 완료 기준이 있는 활성 Ghost-ALICE 세션을 사용해 주세요. 해당 프로젝트 디렉터리에서 저장소 bridge를 실행하며, 다른 위치의 스크립트를 사용하시면 절대경로를 지정해 주세요. 모든 자리표시자는 현재 훅 영수증이나 확인 결과로 바꿔 주세요. 예시 자체가 승인을 부여하지는 않습니다.

- 실행 상태를 기록하지 않고 현재 입력과 자동 Stop 대상을 확인해 주세요.

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --run-dir "<project>/.autopilot" --check
```

- 결과의 `latest_input_event.event_id`와 `automatic_target.run_dir`을 읽어 주세요. 첫 번째 `--run-dir`은 확인용 후보이며 승인 등록 대상이 아닙니다. 정확하게 선택된 디렉터리와 캡처한 입력 영수증으로 다시 확인해 주세요.

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --input-event-id "<checked-event-id>" \
  --run-dir "<automatic_target.run_dir>" --check
```

- `automatic_target.matches_requested_run: true`, 일치하는 플랫폼·세션과 같은 입력을 확인해 주세요. 목표, 계획, 기준, 허용 변경과 예산을 검토한 뒤 해당 세션에서 실제로 승인해 주세요. 그 승인에 연결된 영수증을 사용하며, 승인 과정에서 입력이 바뀌면 새 영수증으로 다시 확인해 주세요. 명시적인 `GHOST_ALICE_AUTOPILOT_RUN_DIR` 지정은 유지해 주세요. 소유권 충돌이 다른 실행으로 경로를 바꿀 권한을 주지는 않습니다.
- 실제 승인 이후 정확하게 선택된 실행에 승인 항목을 등록해 주세요. `--approval-evidence-json`에는 실제 승인 결정(`GO`, `approve` 또는 `approved`)과 해당 승인을 식별하는 비어 있지 않은 출처가 있어야 합니다. 자리표시자를 실제 JSON 승인 기록으로 바꿔 주세요.

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --input-event-id "<approved-checked-event-id>" \
  --run-dir "<automatic_target.run_dir>" \
  --current-work-item-id "<approved-work-item-id>" \
  --plan-path "<approved-plan-path>" \
  --remaining-steps <approved-step-budget> \
  --allowed-surface "<approved-surface>" \
  --approval-evidence-json '<actual-approval-evidence-json>'
```

Claude 세션에서는 `--platform claude`를 사용해 주세요. Bridge는 선택된 `run_dir`과 등록 결과를 알립니다. Stop 이벤트의 작업 지속은 정확한 실행의 현재 승인, 일시정지, 예산, 작업 상태와 근거에 따라 결정됩니다. 명령 예시가 특정 실행 ID나 작업 항목을 보장하지는 않습니다.

상태 위치와 작업 파일에는 확인 결과나 완료 영수증의 선택된 `run_dir`을 사용해 주세요. 승인된 `consistency-decision.json`과 `conduct-plan.json`은 해당 디렉터리에 두며 후보 파일은 진단 자료로만 사용합니다. 완료에는 승인 스키마, 현재 `approval_generation`, 기준에 연결된 통과 근거와 Core 완료 영수증이 필요합니다. 마이그레이션 후에는 SQLite가 기준입니다. 오래된 `approved-run.json`이나 `tasks.jsonl` 내보내기 자료를 수정하거나 삭제해도 저장된 실행이 바뀌거나 중지되지 않습니다. 전체 승인 스키마와 결정 명령은 [일관성 결정 계약](./addons/autopilot-mode/skill/SKILL.md#consistency-decisions)에서 확인해 주세요.

## 일시정지, 재개, 중지

- 프로젝트의 모든 기본 세션 실행을 일시정지하시려면 `<project>/.autopilot/OFF`를 만들어 주세요.
- 선택한 실행만 일시정지하시려면 `<automatic_target.run_dir>/OFF`를 만들어 주세요.
- 재개하시려면 직접 만든 해당 `OFF` 표시를 제거해 주세요. 선택 실행의 표시가 없어도 프로젝트 수준의 표시가 남아 있으면 기본 실행은 계속 일시정지됩니다.

프로젝트 디렉터리에서 전체 일시정지와 재개 명령은 다음과 같습니다.

```bash
touch .autopilot/OFF
```

재개:

```bash
rm .autopilot/OFF
```

원하시는 동작의 명령만 실행해 주세요. 개별 실행에는 `.autopilot/` 대신 확인한 선택 디렉터리를 사용해 주세요.

작업을 중지하시려면 에이전트에게 명시적으로 중지를 요청하고 적절한 `OFF` 표시를 유지하여 어댑터의 후속 실행을 막아 주세요. 나중에 다시 승인하기 전에는 정확하게 선택된 실행을 확인해 주세요. 마이그레이션된 실행을 중지하려고 JSON 내보내기 자료를 수정하거나 삭제하지 마세요.

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
- adapter는 선택한 프로젝트 로컬 실행 상태를 변경하고 기준별 완료 근거를 Core SQLite 원장에 기록하며 continuation payload를 출력합니다.
- continuation payload는 실행 중인 agent가 멈추기 전에 promoted `<selected-run-dir>/consistency-decision.json`을 남기도록 `before-stop` contract를 포함합니다.
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
