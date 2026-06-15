<div align="center">

# ghost-alice-autopilot

Claude Code와 Codex CLI를 위한 검증-게이트 자율 작업 드레인.

Language: [🇺🇸 English](./README.md) | 🇰🇷 한국어

</div>

`autopilot-mode`는 [Ghost-ALICE OS](https://github.com/AidALL/ghost-alice)의 첫 공식 애드온이다. 에이전트가 매 턴 이후 다음 큐 작업을 자동으로 이어받게 한다. 단, 그 턴이 Ghost-ALICE 완료 게이트를 통과했을 때만 발동한다. 검증 게이트 위에 올라타는 자율성 증폭기이며, 게이트를 약화시키지 않는다.

이 애드온은 완전히 자기완결적이다. Ghost-ALICE 코어나 그 인스톨러를 수정하지 않는다. 스킬은 코어가 설치하고, 훅 라이프사이클은 이 애드온이 자체적으로 제공한다.

## 안전한 이유 (검증 락)

오토파일럿은 Ghost-ALICE 검증 게이트가 그 정지를 유효한 완료 주장으로 허용할 때만 전진한다. Stop 훅은 코어 게이트의 검증 함수(`completion_check_validator.validate_completion_text`)와 transcript 헬퍼를 그대로 가져다 쓴다. 따라서 "검증됨"의 정의가 게이트와 동일하며, 문자열 대용물이 아니다. 코어 게이트 로직을 import하지 못하면 전진을 거부한다(fail-safe). 검증 Stop 훅과 오토파일럿 Stop 훅은 상호배타다. 완료블록이 무효면 게이트가 막고, 유효할 때만 오토파일럿이 발동한다.

## 요구사항

- [Ghost-ALICE OS](https://github.com/AidALL/ghost-alice) 코어 설치(이 애드온은 `~/ghost-alice/_shared`의 코어 검증 게이트를 import한다).
- Python 3.11+.
- Claude Code 그리고/또는 Codex CLI(Codex 훅은 v0.114+).

## 설치

두 단계다. 먼저 Ghost-ALICE 코어 인스톨러로 스킬을 설치하고, 이 레포의 스크립트로 훅을 배선한다.

```bash
# 1) 스킬 설치 (코어 인스톨러; install-state에 기록됨)
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot --platform claude

# 2) 오토파일럿 Stop + UserPromptSubmit 훅 배선 (이 레포)
bash install-hooks.sh --platform claude
```

Codex는 `claude`를 `codex`로 바꾸고, `--platform`을 생략하면 감지된 모든 플랫폼을 대상으로 한다. 훅은 세션 시작 시 로드되므로 새 세션부터 적용된다.

## 프로젝트에서 켜기

오토파일럿은 cwd-게이팅이다. 현재 프로젝트에 큐 파일이 없으면 완전히 inert다.

```bash
mkdir -p .autopilot
printf '%s\n' '{"task":"첫 작업"}' '{"task":"둘째 작업"}' > .autopilot/queue.jsonl
```

상태는 `<project>/.autopilot/` 아래에 있다.

| 파일 | 역할 |
|---|---|
| `queue.jsonl` | 작업 목록, 한 줄에 하나(`{"task":"..."}` 또는 평문). 존재 = opt-in. |
| `OFF` | 존재하면 오토파일럿 일시정지(큐는 보존). |
| `inject_count` | 현재 배치의 자동 주입 카운터. 새 유저 입력마다 리셋. |

일시정지: `touch .autopilot/OFF`. 재개: `rm .autopilot/OFF`. 배치당 상한: `MAX_INJECTIONS`(기본 25). `.autopilot/queue.jsonl`이 없으면 훅은 완전히 inert다.

## 제거

```bash
bash uninstall-hooks.sh --platform claude                     # 훅 제거 (이 레포)
bash <ghost-alice>/install.sh --uninstall --platform claude   # 스킬 제거 (코어 인스톨러)
```

## 라이선스

Apache-2.0. [LICENSE](./LICENSE), [NOTICE](./NOTICE) 참고.
