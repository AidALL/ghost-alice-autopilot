# ghost-alice-autopilot

승인된 자율 연속 실행을 위한 공식 Ghost-ALICE 애드온.

Language: [English](./README.md) | Korean

`autopilot-mode`는 Ghost-ALICE가 승인된 실행을 작업 단위로 이어가게 하는 애드온이다. 에이전트 stop 이벤트 이후 프로젝트의 `.autopilot/` 상태를 읽고, 다음 ready 작업을 선택하고, 해당 작업을 running으로 표시한 뒤 다음 continuation message를 출력한다.

## 이 애드온이 하는 일

- `autopilot-mode` skill을 설치한다.
- Ghost-ALICE installer를 통해 core-owned `[adapter:autopilot-mode] continue` hook을 등록한다.
- `scripts/autopilot_start_run.py`로 사용자 승인 GO spec에서 approved run state를 만든다.
- `scripts/autopilot_consistency_decision.py`로 completion-check output을 다음 run decision으로 변환한다.
- 프로젝트 로컬 `.autopilot/` 실행 상태를 읽는다.
- no-op payload 또는 다음 work-item message를 출력한다.
- adapter event를 `.autopilot/events.jsonl`에 기록한다.

이 애드온은 실행 시작 여부를 결정하지 않는다. session-intent analysis, task routing, 사용자의 명시적 GO 결정이 approved run state를 만든다.

## 동작 방식

런타임 흐름:

1. Ghost-ALICE core installer가 이 애드온을 설치하고 privileged adapter hook을 배선한다.
2. session intent와 task routing이 사용자의 명시적 GO 결정을 확인한 뒤 `autopilot_start_run.py`가 `.autopilot/approved-run.json`과 `.autopilot/tasks.jsonl`을 쓴다.
3. 에이전트가 멈추면 adapter가 `.autopilot/`을 읽는다.
4. 실행이 approved, running, 예산 내 상태이고 ready task가 있으면 adapter가 해당 task를 `running`으로 표시한다.
5. adapter가 다음 work item이 담긴 continuation payload를 출력한다.
6. work item이 completion-check output을 만들면 `autopilot_consistency_decision.py`가 `.autopilot/consistency-decision.json`을 쓴다.
7. 다음 stop 이벤트에서 adapter는 다른 ready item을 선택하기 전에 그 decision을 적용한다.
8. 실행이 승인되지 않았거나, pause/stop 상태이거나, 예산이 없거나, ready item이 없으면 no-op payload를 반환한다.

기본 run directory:

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
  consistency-decision.json
  consistency-decision.applied.json
  events.jsonl
  OFF
```

## 요구사항

- privileged adapter를 지원하는 Ghost-ALICE core 0.1.3 이상.
- Python 3.11+.
- Ghost-ALICE core installer로 설치된 Claude Code 또는 Codex hook.

Ghost-ALICE core 0.1.3 미만에는 이 애드온을 설치하지 않는다. 오래된 core installer는 skill만 복사하고 privileged adapter를 배선하지 않을 수 있다. 이 설치는 inert 상태이므로 업그레이드 전에 제거한다.

## 설치

로컬 checkout:

```bash
bash <ghost-alice>/install.sh \
  --platform claude \
  --addon-source /path/to/ghost-alice-autopilot
```

Git URL source:

```bash
bash <ghost-alice>/install.sh \
  --platform claude \
  --addon-source https://github.com/AidALL/ghost-alice-autopilot.git
```

Codex는 `--platform codex`를 사용한다.

설치 상태 확인:

```bash
bash <ghost-alice>/install.sh --platform claude --status
```

## 바로 실행해 보기

프로젝트 디렉토리에서 approved run을 만든다.

```bash
python3 /path/to/ghost-alice-autopilot/addons/autopilot-mode/skill/scripts/autopilot_start_run.py <<'JSON'
{
  "run_id": "demo-run",
  "scope": {"summary": "Demo autopilot continuation"},
  "budget": {"remaining_steps": 2},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"},
  "tasks": [
    {
      "id": "unit-1",
      "focus_layer": "micro",
      "depends_on": [],
      "prompt": "Implement the first approved demo unit.",
      "acceptance_criteria": ["the next continuation message names unit-1"],
      "allowed_surface": ["src/..."]
    }
  ]
}
JSON
```

다음 agent stop 이벤트 이후 adapter는 아래 형태의 continuation message를 출력한다.

```text
[autopilot]
run: demo-run
work-item: unit-1
focus-layer: micro
allowed-surface:
- src/...
acceptance-criteria:
- the next continuation message names unit-1
prompt:
Implement the first approved demo unit.
```

## 동작 영상 예시

권장 영상 흐름:

1. 로컬 checkout 또는 Git URL에서 설치한다.
2. 명시적 GO spec으로 `autopilot_start_run.py`를 실행한다.
3. 생성된 `.autopilot/approved-run.json`과 `.autopilot/tasks.jsonl`을 보여준다.
4. 에이전트 턴을 끝내고 `[autopilot]` continuation message를 보여준다.
5. per-addon uninstall을 실행하고 adapter hook이 제거된 상태를 보여준다.

녹화 후 아래 위치에 asset을 추가한다.

```text
docs/demo/autopilot-mode.mp4
```

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
  --platform claude \
  --uninstall --addon autopilot-mode
```

Codex는 `--platform codex`를 사용한다.

Ghost-ALICE 전체 제거는 core full-uninstall 경로를 사용한다.

```bash
bash <ghost-alice>/install.sh --platform claude --uninstall
```

## 제한 및 신뢰 메모

- 애드온 설치는 런타임 활성화가 아니다.
- adapter는 인자를 받지 않는다.
- adapter는 `.autopilot/` 상태를 읽고 continuation payload만 출력한다.
- tool denial, installer policy, privileged adapter allowlist, hook marker, runner namespace, hook install/remove 동작은 Ghost-ALICE core가 소유한다.
- 이 애드온 패키지는 skill content와 adapter implementation을 소유한다.

## 저장소 구조

```text
addons-manifest.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_mode.py
  skill/adapters/autopilot_state.py
  skill/scripts/autopilot_start_run.py
  skill/scripts/autopilot_consistency_decision.py
  skill/scripts/autopilot_dogfood_runner.py
tests/
```

## 라이선스

Apache-2.0. [LICENSE](./LICENSE), [NOTICE](./NOTICE) 참고.
