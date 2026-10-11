# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot 로고" width="360">
</p>

완료 근거를 기록하고 승인된 후속 작업을 이어가는 공식 Ghost-ALICE 애드온입니다.

언어: [English](./README.md) | 한국어

Ghost-ALICE Autopilot 0.5.1에는 현재 response-route·completion·공유 SQLite API를 제공하는 Ghost-ALICE Core 0.5.1 이상과 Python 3.11+가 필요합니다. 설치만으로 실행 권한이 생기지는 않습니다.

## 역할

현재 세션 assistant는 현재 hook receipt가 식별하는 native 대화에서 응답하는 모델입니다. 위임된 assistant는 명시적으로 맡긴 하위 작업이 있을 때만 별도로 존재합니다. Core와 Ghost-ALICE Autopilot은 지침과 Python 코드로 이루어진 책임 영역이며, 별도로 사고하는 에이전트가 아닙니다.

현재 세션 assistant는 Ghost-ALICE 지침에 따라 개별 주장을 원본 근거와 대조한 뒤 응답 전체를 사용자 목적과 제약에 비추어 판단합니다. 실제 shell·Python·업무 도구 호출은 native host에 요청합니다. Ghost-ALICE 코드는 검토 입력을 저장하고 assistant가 기록한 판단의 바인딩을 인증합니다. Assistant는 이어서 Ghost-ALICE Autopilot 지침과 실제 사용자 권한을 적용하여 필요한 후속 작업이 있는지 판단하며, Ghost-ALICE Autopilot adapter 코드는 지원되는 결과를 기록하고 저장된 승인 계획에서 실행 가능한 작업을 선택합니다.

| 실제 수행 주체 | 입력 | 수행 동작 | 출력 |
| --- | --- | --- | --- |
| 현재 세션 assistant | Hook receipt, 사용자 권한, Ghost-ALICE Core 절차로 검토한 결과 | task-router·boundary-contract·verification-before-completion을 적용하고 Ghost-ALICE Autopilot 지침에 따라 추가 작업 판단과 업무 도구 호출을 요청합니다. | 업무 결과, 원본 기준별 proof, 승인된 후속 계획 또는 부분 응답 |
| Ghost-ALICE Core 검증 코드 | Assistant가 기록한 의미·근거 검토와 정확한 바인딩 | 검토·출처 연결, admission과 전달 선행 조건을 인증합니다. | Completion assessment와 응답 안내 |
| Ghost-ALICE Autopilot adapter 코드 | Ghost-ALICE Core completion assessment와 승인된 계획 | advance_continuation이 다음 작업을 선택하고 dispatch를 기록하며 안내를 구성합니다. | Continuation payload와 필요한 경우 _format_payload_for_hook이 반환하는 block/reason |
| Native host | Assistant의 도구 요청과 훅 안내 | 권한을 집행하고 도구·훅을 호출하며 지원되는 경우 수신 대화에 훅 안내를 제공합니다. | 현재 세션 assistant에 전달할 도구 결과·후속 안내와 사용자에게 표시할 응답 |

~~~mermaid
flowchart TD
  U[사용자가 목적·제약과 실제 승인 범위를 제시합니다] -->|목적·제약과 부여한 권한| S[응답 모델이 micro·meso·macro·meta 판단 범위를 선택하고<br/>검증 깊이를 별도로 선택합니다]
  S -->|승인된 도구 요청| T[Native host가 허용된 도구를 실행하고 실제 결과를 반환합니다]
  T -->|원본 결과와 출처 위치| D[응답 모델이 정확한 전체 답변 원고를 작성하고 원본 출처를 지정합니다]
  D -->|response-file·source 경로를 담은 prepare-response shell 도구 요청| HP[Native host가 Ghost-ALICE completion_assessment.py를 호출합니다]
  HP -->|파일에서 읽은 response_text와 source_paths| E[Ghost-ALICE prepare_response_review가 정확한 입력을 저장하고<br/>candidate_reference·bindings·sources·response_binding을 반환합니다]
  E -->|정확한 원고·원본 출처 사본·reference| V[응답 모델이 1단계에서 개별 주장을 원본 근거와 대조합니다]
  V -->|주장별 판단과 정확한 원고| V2[응답 모델이 2단계에서 주장별 판단과 목적·제약을 사용하여<br/>정확한 응답 전체를 판단합니다]
  U -->|목적과 필수 조건| V2
  V2 -->|candidate reference·judgment-file을 담은 record-response shell 도구 요청| HR[Native host가 Ghost-ALICE completion_assessment.py를 호출합니다]
  HR -->|저장된 candidate reference와 claims·response 판단| C[Ghost-ALICE record_response_review가 바인딩을 인증하고 두 판단을 함께 저장합니다<br/>유효한 unsupported 결과도 보존합니다]
  C -->|인증된 검토 결과와 남은 요구 사항| J[응답 모델이 Ghost-ALICE Autopilot 지침에 따라<br/>승인된 남은 작업의 필요성을 판단합니다]
  U -->|실제 승인과 그 경계| J
  U -->|이미 승인된 조건부 작업 계획| P
  J -->|실제 권한 안에서 필요한 작업| P[응답 모델이 Stop 전에 정확한 작업과 의존 관계를 등록합니다]
  J -->|정당한 승인 행동이 없음| N[응답 모델이 미해결 요구를 보존하거나 비필수 작업을 제외합니다]
  P -->|현재 source·event, items, 실제 승인과 허용 범위를 담은 Python 도구 요청| HT[Native host가 설치된 autopilot_continuation을 import하고 register_continuation_plan을 호출합니다]
  HT -->|검증된 계획 입력| Q[Ghost-ALICE Autopilot이 승인 계획을 저장하고<br/>admitted와 plan_id를 반환합니다]
  C -->|두 검토 단계 모두 supported인 경우 정확한 response_text| R[응답 모델이 검토된 답변을 native host에 제출합니다]
  C -->|unsupported 또는 unavailable 결과에는 response_text 없음| N
  R -->|제출 답변과 현재 hook receipt| K[Native host가 완료 guard와 Ghost-ALICE Autopilot Stop 훅을 각각 호출합니다]
  K -->|답변과 receipt를 담은 완료 guard 호출| G[설치된 Ghost-ALICE Core 완료 guard가 제출 답변을 저장된 검토와 연결하여 인증합니다]
  K -->|답변과 receipt를 담은 Ghost-ALICE Autopilot Stop 호출| B[Ghost-ALICE Autopilot apply_completion_v2가 Core가 선택한 완료 근거만 접수하고<br/>인증된 publication을 적용하거나 재사용합니다]
  C -->|저장된 검토와 정확한 근거 바인딩| G
  C -->|동일한 저장 검토와 정확한 근거 바인딩| B
  B -->|적용된 기준 상태와 기록 결과| A[Ghost-ALICE Autopilot advance_continuation이 계획에서 적격 승인 작업을 선택합니다]
  Q -->|저장된 작업·승인 경계·의존 관계| A
  A -->|적격 작업과 변경 없는 상태의 dispatch 검사| F[Ghost-ALICE Autopilot 코드가 dispatch를 기록하고 Stop 안내를 구성합니다]
  A -->|적격 작업 없음과 기록된 상태| W[Ghost-ALICE Autopilot 코드가 complete·waiting·parked 상태를 반환합니다]
  F -->|Hook receipt의 대화를 대상으로 한 작업 요청| H[Native host가 수신 대화가 지정된 Stop 안내를 받습니다]
  W -->|Hook receipt의 대화를 위한 후속 작업 상태| H
  H -->|Host가 해당 대화에 작업 요청을 전달한 경우| X[그 대화의 다음 응답을 생성하는 모델이 작업 요청을 받습니다]
  X -->|기존 권한 안에서 받은 작업 요청| S
  R -->|제출된 정확한 답변| O[Native host가 사용자 화면에 답변을 전달합니다]
  G -->|완료 guard 결과| O
  H -->|Stop 후속 작업 결과| O
  C -.->|검토 기록 이벤트| L[감사 코드가 전체 이벤트를 기록합니다]
  A -.->|후속 작업과 dispatch 이벤트| L
~~~

완료 판단의 책임은 [Ghost-ALICE Core 구조](https://github.com/AidALL/ghost-alice/blob/main/architecture.md)에, Ghost-ALICE Autopilot 명령·기록 스키마·대상 선택은 [운영 스킬](./addons/autopilot-mode/skill/SKILL.md)에 정의되어 있습니다.

현재 세션 assistant는 [대조 그림](https://github.com/AidALL/ghost-alice/wiki/design-philosophy-floor-not-ceiling_ko#대조와-피드백)과 [판단 범위 그림](https://github.com/AidALL/ghost-alice/wiki/operating-model-fallback-and-escalation_ko#판단-범위와-검증-부담)에 따라 남은 작업을 판단합니다. Assistant는 사용자에게 질문하지 않고도 작업의 필요성이나 전제를 재검토할 수 있습니다. 다음 행동에 사용자만 내릴 수 있는 결정이 필요하고 그 결정이 아직 없을 때 사용자에게 질문합니다. 목적과 필수 의존 관계에 영향이 없는 미해결 사항 때문에 복구 작업을 만들지는 않습니다. 근거가 없는 필수 조건은 미해결로 유지합니다. Ghost-ALICE Autopilot은 Ghost-ALICE Core 결과와 저장된 승인 계획을 사용하며 주장의 의미 검토를 반복하거나 새 권한을 만들지 않습니다. 위 실행 구조 그림은 Stop 전 계획 등록과 Stop에서의 결정적 선택을 구분합니다. Stop은 없는 계획을 만들도록 모델을 호출하지 않습니다. 판단 범위와 검증 부담은 서로 독립적입니다.

Core는 검토 기록을 저장하고 Ghost-ALICE Autopilot은 publication·계획·dispatch 기록을 별도로 저장합니다. Native host는 완료 guard와 Ghost-ALICE Autopilot Stop 훅을 각각 호출하며 두 경로는 공유된 Ghost-ALICE Core 평가를 사용합니다. 어느 코드 경로도 assistant의 업무를 직접 수행하지 않습니다. Hook receipt는 대화를 식별합니다. Host가 그 대화에 안내를 전달하면 해당 대화의 다음 응답을 생성하는 모델이 이를 받으며, 동일한 모델 프로세스의 재개를 보장하지는 않습니다.

## 설치와 제거

운영자는 Ghost-ALICE Core checkout에서 Ghost-ALICE Core 설치 프로그램을 실행합니다.

~~~bash
bash install.sh --addon autopilot
bash install.sh --platform codex --addon autopilot
~~~

운영자는 --platform claude로 Claude를 선택합니다. 개발 checkout은 bash `<ghost-alice>/install.sh` --addon-source `<addon-repository>`로 설치하고, 설치 상태는 bash `<ghost-alice>/install.sh` --platform codex --status로 확인합니다.

Ghost-ALICE Core 설치 프로그램은 Ghost-ALICE Autopilot adapter를 등록하고 source/hash 바인딩을 확인하며 훅을 설치합니다. Ghost-ALICE Autopilot에는 독립 설치 프로그램이 없습니다. 플랫폼 설정은 [Ghost-ALICE Core 설치 안내](https://github.com/AidALL/ghost-alice/blob/main/docs/getting-started/installation.md)를 참고해 주세요.

운영자는 bash `<ghost-alice>/install.sh` --platform codex --uninstall --addon autopilot-mode로 애드온을 제거합니다. Claude에는 --platform claude를 사용합니다. Ghost-ALICE Core 전체 제거 명령은 bash `<ghost-alice>/install.sh` --uninstall입니다.

설치된 어댑터는 `GHOST_ALICE_RUNTIME_SHARED_DIR`로 지정한 Core 설치본을 사용하며, 그 설치본의 원장과 공유 정책 모듈을 함께 불러옵니다. `GHOST_ALICE_CORE_ROOT`는 소스 위치를 유지하지만 지정한 런타임을 대체하지 않습니다. 지정한 설치본을 사용할 수 없으면 소스로 대체하지 않고 사용 불가로 반환합니다. 런타임을 지정하지 않은 명시적 소스 CLI 실행은 선택한 소스 공급자를 사용합니다.

## 완료 기록과 응답 전달

현재 세션 assistant는 task-router로 현재 승인된 작업을 선택하고 [Core 응답 검토·publication 절차](https://github.com/AidALL/ghost-alice/blob/main/coding-convention/verification-before-completion/references/autopilot-publication.md)를 따릅니다. 해당 절차는 prepare-response·record-response 호출, 개별 주장과 응답 전체의 구별된 판단, response_text 제출, 원본 근거·시각 보존과 응답 전달을 정의합니다.

등록된 pretool은 선택된 작업의 기록 출처를 준비합니다. Native Stop에서 Ghost-ALICE Autopilot adapter 코드는 Core가 인증한 검토 결과를 받아 publication을 기록하고 보호된 기준 쓰기를 요청합니다. completion_recording은 기록과 기준 적용 결과이며, continuation은 승인 계획의 후속 작업 상태입니다. Publication 접수, 부분 기준 적용, 전체 작업 완료와 사용자 화면 전달은 각각 별도의 결과입니다. 기록 오류는 해당 의존 기록만 보류하며, 필요한 정정과 전달은 Core 소유 절차를 따릅니다.

## 승인된 후속 작업과 queue

현재 대화의 모델은 검토 결과와 사용자 권한으로 후속 작업의 필요성을 판단합니다. 이미 승인된 조건부 계획은 현재 결과의 Stop 적용 전에 등록할 수도 있습니다. [계획 등록 절차](./addons/autopilot-mode/skill/references/continuation.md#register-the-approved-plan)는 register_continuation_plan의 입력과 native host Python 호출을 정의합니다. Ghost-ALICE Autopilot의 advance_continuation은 적용된 기준과 의존 관계로 적격 작업을 선택하고 dispatch를 기록합니다. Host가 해당 대화에 안내를 전달하면 그 대화의 다음 응답을 생성하는 모델이 승인된 작업을 수행합니다. Ghost-ALICE Autopilot 코드는 업무 도구를 실행하거나 assistant를 생성하지 않습니다.

명시적 v1 queue 호출에는 [Approved queue API](./addons/autopilot-mode/skill/SKILL.md#approved-queue-api)를 사용합니다. 실제 승인 근거와 현재 receipt, 경계와 예산이 필요하며, [Consistency decisions](./addons/autopilot-mode/skill/SKILL.md#consistency-decisions)에서 runtime 프로토콜을 구분합니다.

## 상태, 대상과 일시정지

SQLite와 공개 storage API가 기준입니다. JSON export와 inbox는 계약이 정한 가져오기·내보내기 역할을 유지하며 export 수정으로 저장된 권한을 바꿀 수 없습니다. Ghost-ALICE Core source 모듈, runtime 모듈, ledger, project, 설치된 skill은 명시적으로 구분된 위치를 사용합니다. 잘못된 바인딩은 다른 authority를 찾지 않고 실패로 처리합니다.

신뢰된 절대 GHOST_ALICE_GHOST_ROOT가 있는 managed v2 세션은 `<ghost-root>/autopilot/sessions/<platform>/<session-id>/`를 기본 위치로 사용합니다. 기존 exact-owner receipt는 cwd 변경 후에도 원래 대상을 유지합니다. 현재 세션 assistant는 반환된 automatic_target.run_dir 또는 receipt run_dir를 사용합니다. 엄격한 GHOST_ALICE_AUTOPILOT_RUN_DIR과 절대 GHOST_ALICE_AUTOPILOT_CWD override는 [대상 계약](./addons/autopilot-mode/skill/SKILL.md#target-and-state-inspection)을 따릅니다.

| 일시정지 표시 | 적용 대상 |
| --- | --- |
| `<project>/.autopilot/OFF` | 해당 업무 위치의 기본 실행 |
| `<ghost-root>/autopilot/OFF` | Managed runtime 실행 |
| `<selected-run-dir>/OFF` | 선택된 실행 |

Ghost-ALICE Autopilot adapter 코드는 pretool·준비·publication·Stop에서 일시정지 표시를 따릅니다. 승인된 재개 시 현재 세션 assistant가 해당 표시를 제거하며, 남은 표시는 계속 적용됩니다. 명시적 중지 시 현재 세션 assistant는 표시를 유지하고 공개 transaction API로 승인된 queue의 stopped 상태를 기록합니다.

## 호환성과 저장소 구성

[compatibility-matrix.json](./compatibility-matrix.json)은 대상별 근거를 기록합니다. not-run 대상은 미검증 상태이며 source 검사만으로 설치나 native host 적용을 입증할 수 없습니다.

- addons-manifest.json: 애드온 등록과 최소 Ghost-ALICE Core 버전입니다.
- addons/autopilot-mode/skill/SKILL.md: 운영 계약입니다.
- addons/autopilot-mode/skill/adapters/: 기록과 후속 작업 구현입니다.
- addons/autopilot-mode/skill/scripts/: completion, queue, governance 진입점입니다.
- scripts/autopilot_session_bridge.py: 저장소 bridge wrapper입니다.
- tests/: source 계약과 동작 검사입니다.

## 라이선스

Apache-2.0입니다. [LICENSE](./LICENSE)와 [NOTICE](./NOTICE)를 확인해 주세요.
