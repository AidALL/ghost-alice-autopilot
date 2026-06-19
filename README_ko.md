# ghost-alice-autopilot

승인된 자율 연속 실행을 위한 공식 Ghost-ALICE 애드온.

Language: [English](./README.md) | Korean

`autopilot-mode`는 Ghost-ALICE P6 privileged adapter를 설치하는 애드온이다. 에이전트 stop 이벤트 이후 다음 승인 작업을 이어가지만, 프로젝트 로컬 approved-run 상태가 사용자의 명시적 자율 실행 승인을 담고 있을 때만 동작한다.

이 저장소는 애드온 패키지를 소유한다. Ghost-ALICE 코어는 installer policy, privileged adapter allowlist, hook marker, runner namespace, hook install/remove 동작을 소유한다.

## 요구사항

- P6 privileged adapter를 지원하는 Ghost-ALICE 코어.
- Python 3.11+.
- Ghost-ALICE 코어 인스톨러로 설치된 Claude Code 또는 Codex hook.

## 설치

Ghost-ALICE 코어 인스톨러로 설치한다. P6에서는 별도 애드온 hook installer를 실행하지 않는다.

```bash
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot --platform claude
```

Codex는 `--platform codex`를 사용한다. 코어 인스톨러가 이 저장소의 `addons-manifest.json`을 읽고 skill을 설치하며, core-owned `[adapter:autopilot-mode] continue` hook을 배선한다.

## 런타임 활성화

설치만으로는 동작하지 않는다. 실행을 활성화하려면 프로젝트에 승인 상태를 만든다.

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
```

`approved-run.json`은 명시적 GO 경계를 담아야 한다.

```json
{
  "schema_version": "autopilot-run.v1",
  "run_id": "run-1",
  "approved": true,
  "status": "running",
  "scope": {"summary": "승인된 작업 범위"},
  "budget": {"remaining_steps": 3},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"}
}
```

`tasks.jsonl`은 durable source of truth다. adapter는 task status와 dependency에서 ready queue를 파생하며, 파일에서 줄을 pop하지 않는다.

일시정지:

```bash
touch .autopilot/OFF
```

재개:

```bash
rm .autopilot/OFF
```

중지는 `approved-run.json`의 `status`를 `stopped`로 바꾸거나, `approved`를 false로 바꾸거나, `budget.remaining_steps`를 0으로 만들거나, `approved-run.json`을 제거하여 수행한다.

## Adapter 동작

- 인자를 받지 않는다.
- 기본 run directory는 `<cwd>/.autopilot/`이다.
- `GHOST_ALICE_AUTOPILOT_RUN_DIR`로 명시적 run directory를 지정할 수 있다.
- `consistency-decision.json`이 있으면 소비한다.
- `events.jsonl` 감사 기록을 쓴다.
- no-op payload 또는 다음 work-item continuation message만 출력한다.

## 제거

Ghost-ALICE 코어 uninstall 경로를 사용한다.

```bash
bash <ghost-alice>/install.sh --uninstall --platform claude
```

코어 uninstaller가 managed adapter hook을 제거하고, addon-owned 파일은 코어 addon uninstall policy에 따라 보존한다.

## 구조

```text
addons-manifest.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_mode.py
  skill/adapters/autopilot_state.py
tests/
```

## 라이선스

Apache-2.0. [LICENSE](./LICENSE), [NOTICE](./NOTICE) 참고.
