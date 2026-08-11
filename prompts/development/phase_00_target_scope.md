# Phase 0 — Target Scope Contract

## User prompts

```text
그럼 파일 수정해줘
```

```text
그럼 이제 단계별로 하나씩 해줘
```

## Operational interpretation

검토에서 확인된 제출·재현성 누락을 명세에 반영하고, Canonical 순서를 Phase 0부터
하나씩 구현한다. 다음 Phase로 이동하기 전에 현재 Phase의 완료 조건을 실제
artifact와 검사로 입증한다.

## Phase-specific implementation prompt

```text
공식 raspberrypi/userland 저장소의 분석 대상을 full commit SHA로 고정한다.
고정 Git tree의 모든 tracked file을 분류하되, .c/.h/.cpp는 테스트·예제·bundled
open-source를 포함해 path exclusion 없이 전부 in-scope로 둔다. Target source는
제출 저장소에 vendoring하지 않는다. File manifest와 scope hash를 결정적으로
생성하고, origin/commit/clean tree/count/hash drift를 fail-closed로 검증한다.
Build/preprocessor 가정과 trust-boundary 가설을 별도 문서로 남긴다.
```

## Decisions made

- Upstream commit: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
- In-scope: tracked `.c` 284, `.h` 367, `.cpp` 3, 합계 654
- Path exclusions: 0
- Out-of-scope tracked artifacts: 176, 각각 사유 기록
- Target checkout: `.target-src/userland`, Git ignore 및 제출 제외
- Lock encoding: YAML 1.2에 유효한 strict JSON
- Scope semantic change: 새 Experiment ID와 downstream artifact 전부 재생성

## Verification result

- Target origin, exact detached commit, clean worktree, submodule 0 검증
- Tracked 830 = in-scope 654 + out-of-scope 176 검증
- Manifest JSON/JSONL parsing과 반복 verify 통과
- PowerShell 생성기와 구현을 공유하지 않는 Python verifier로 Git tree, Schema,
  Manifest, 두 hash를 독립 재계산해 통과
- Verify 전후 artifact SHA-256 불변 확인
- Target file-list SHA-256:
  `fb00da1a82fcd1e02b436c4f58cbd8391094901da8450b9881063db3558e6057`
- Scope SHA-256:
  `2d7910c98cd57192f9c80436d18d39be247a8b8e6705b98f831ee747b1d1d50a`
