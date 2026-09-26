# Ghost-ALICE Autopilot v0.3.0 릴리스 노트

날짜: 2026-09-26

Language: [English](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/docs/release/2026-09-26-release-notes.md) | 한국어

이번 릴리스에서는 Autopilot 제품 버전을 Ghost-ALICE core `0.3.0`과 맞추고, 보류 중인 상태 변경을 적용하기 전에 선택된 세션의 현재 의도를 확인하도록 개선했습니다. 영문 대응 문서는 GitHub 릴리스 본문의 원본이며, `VERSION`·애드온 manifest·변경 이력은 같은 제품 버전을 가리킵니다.

## 변경 내용

- 보류 중인 완료 receipt 소비, conduct plan 반영, 작업 대기열 변경 전에 현재 의도를 확인합니다. 목표가 달라졌거나 선택된 세션의 문맥이 없거나 충돌하면 해당 동작 전에 실행을 보류합니다.
- `agent-runtime` host는 `GHOST_ALICE_PLATFORM=agent-runtime`, `GHOST_ALICE_SESSION_ID`, 절대 경로인 `GHOST_ALICE_SESSION_INTENT_ROOT`를 명시해야 합니다. 선택된 `session-intent-ledger.v1` 기록의 플랫폼과 세션도 일치해야 합니다. 알 수 없는 플랫폼이나 유효하지 않은 문맥을 다른 플랫폼·세션의 기록으로 대신하지 않습니다.
- 실행 환경이 제공한 플랫폼 이름을 세션 binding 비교 전에 일관되게 정규화하여, 유효한 대소문자 혼합 표기 때문에 실행이 보류되지 않도록 수정했습니다. 저장된 원장과 승인 identity는 계속 엄격하게 확인합니다.
- 실행 환경에서 native 플랫폼을 생략하면 승인된 native 플랫폼을 선택한 뒤 실제 현재 세션을 확인합니다. 저장된 플랫폼 binding이 잘못되어 있으면 receipt나 대기열을 변경하기 전에 보류하며, 승인 기록의 세션 ID로 현재 세션을 대체하지 않습니다.
- Codex 문맥을 선택한 경우에는 훅에 명시된 세션 ID, native thread, 일반 세션 값 또는 공유 포인터 순서로 확인합니다. 실행을 처음 만들 때와 이어갈 때 모두 적용합니다. Claude는 상속된 Codex identity를 사용하지 않으며, host 플랫폼을 지정하지 않은 경우에는 기존 native 탐색 동작을 유지합니다.
- 승인된 목표 안에서 요청을 구체화한 경우에는 기존 승인을 유지합니다. 유효한 완료 receipt는 한 번만 소비하며, 다시 진입해도 완료된 전환을 반복하지 않습니다.
- 영문·국문 안내의 설치 방법, 실행 연속성, 호환성, 권장 릴리스 조합을 맞추었습니다. Apache-2.0 라이선스는 유지됩니다.

## 호환성과 업그레이드

의도 기록 및 세션 연결 개선을 함께 사용하시려면 Ghost-ALICE core `0.3.0`과 Autopilot `0.3.0`을 권장합니다. 기존 설치 호환성 하한인 core `0.2.2`는 그대로 유지되며, 권장 조합과는 별개입니다. 내부 schema 버전은 변경하지 않았습니다.

core checkout에서 설치해 주세요. 이 애드온에는 독립적인 루트 설치기가 없습니다.

```bash
bash install.sh --addon autopilot --addon-tag v0.3.0
bash install.sh --status
```

공식 설치 대상은 Claude Code와 Codex입니다. 명시적인 `agent-runtime` 어댑터 계약으로 다른 host가 세션 의도를 제공할 수 있지만, 모델 접근·도구·승인 경계·이벤트 전달은 해당 host가 구현해야 합니다. 이번 릴리스를 설치하는 것만으로 임의의 모델에 이러한 기능이 추가되지는 않습니다.

## 검증과 한계

- [어댑터 상태 회귀 검사](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/tests/test_autopilot_state.py)는 플랫폼 identity, 선택된 의도 문맥, 목표 변경, 작업 대기열 보존, receipt 단회 처리를 검사합니다. 실행 환경의 플랫폼 이름은 소문자·대문자·혼합 표기를 허용하지만, 저장된 원장의 identity는 계속 엄격하게 확인합니다.
- [Privileged adapter 검사](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/tests/test_privileged_adapter.py)와 [호환성 검사](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/tests/test_compatibility_surface.py)는 런타임 경계와 릴리스 패키지 계약을 검사합니다.
- 제공받은 의도 기록과 로컬 기록을 이용한 상태 재생도 수행했지만, 해당 비공개 산출물은 이번 릴리스에 공개하지 않으므로 수치 결과를 이 문서에 싣지 않습니다. 공개 테스트 소스는 회귀 검사 범위를 설명하며, 일반적인 의도 이해 정확도나 독립된 사용자 세션 집합을 나타내지는 않습니다.
- 이번 ledger 검증에서는 Claude 모델의 새 추론 실행을 수행하지 않았습니다. 호환성 매트릭스의 이전 Claude·Codex live 근거를 새 `0.3.0` 근거로 바꾸어 표시하지 않습니다. 매트릭스에 남아 있는 Linux·Windows 검증 공백 때문에 전체 플랫폼 호환성을 주장할 수는 없습니다.

각 검증 범위는 [compatibility-matrix.json](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/compatibility-matrix.json)과 [core 릴리스 노트](https://github.com/AidALL/ghost-alice/blob/v0.3.0/docs/ko/release/2026-09-26-release-notes.md)에서 확인하실 수 있습니다.

## 라이선스

Ghost-ALICE Autopilot은 [Apache-2.0](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/LICENSE) 라이선스의 오픈소스로 유지됩니다. 버전을 맞추더라도 라이선스는 변경하지 않습니다.
