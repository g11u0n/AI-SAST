# Phase 2 — Repository Parser, Structural Chunker, Batch Generator

## User prompt

```text
그럼 이제 단계별로 하나씩 해줘
알아서 해줘 그리고 Phase 단계가 하나 끝날 때마다 github에 커밋도 같이 해줄래
```

## Operational interpretation

Canonical 순서의 다음 미완료 단계인 Phase 2만 구현한다. Phase 0 Target Lock과
Phase 1 Experiment Lock을 입력 계약으로 사용하고, 실제 Userland 전체 corpus에서
완료 기준과 negative test를 모두 통과한 뒤 Phase 2 변경만 Git commit으로 고정한다.
Git remote가 없으므로 local Phase commit을 만들고 remote 연결 후 push한다.

## Phase-specific implementation prompt

```text
source_manifest.jsonl의 654개 path/OID만 입력으로 삼고 local worktree 원문은 읽지
않는다. Git cat-file로 immutable blob을 읽어 OID/type/size/content hash를 검증한다.
C/C++ Tree-sitter parser로 함수/타입/매크로/AST 구조를 분할하고, header는 C/C++
오류 점수를 비교해 grammar를 결정한다. ERROR/MISSING, non-UTF-8 source는 명시적
line-window fallback으로 전체 raw byte를 정확히 한 번 매핑한다. Chunk는 content-
addressed ID, half-open byte range, line/column, raw/rendered hash와 보수적 UTF-8 byte
count를 가진다. 헤더까지 포함한 Evidence payload가 3,840 bytes 이하가 되도록
source-order next-fit Batch를 만들고 모든 Chunk를 정확히 한 번 배치한다. 결과를
보기 전에 잠긴 seed/domain 순위로 3개 Batch를 고정하고 experiment.lock.yaml을
evaluation_frozen으로 전환한다. 독립 검증기, tamper test, 전체 2회 byte-identical
재생성을 통과해야 완료한다.
```

## Decisions made

- Dependencies: `tree-sitter==0.26.0`, `tree-sitter-c==0.24.2`,
  `tree-sitter-cpp==0.23.4`
- Canonical content: locked commit의 Git blob bytes; worktree 접근 금지
- Header grammar: ERROR byte union, MISSING, ERROR count, C-first tie-break 순
- Decode: UTF-8 strict → CP1252 strict; replacement/NUL 금지
- Non-UTF-8: whole-file line-window fallback; syntax error: 정상 구조 단위 보존 후
  diagnostic unit만 line-window fallback
- Coverage: 각 blob `[0, size)`의 non-overlapping exact partition
- Chunk content window: rendered UTF-8 최대 3,000 bytes
- Batch budget: Evidence frame 전체 최대 3,840 UTF-8 bytes
- IDs: domain-separated canonical JSON SHA-256 기반 `C1-*`, `B1-*`
- Packing: global source order의 deterministic next-fit
- Selection: Phase 1의 seed `20260811`/domain ranking 상위 3개

## Verification result

- 654 files = 403 parse success + 251 fallback success + 0 terminal error
- Source bytes 8,594,578 전체 매핑; unmapped/overlap 0
- 26,645 Chunks, 4,695 Batches; oversized Chunk/Batch 0
- UTF-8 strict 실패 5개는 CP1252 fallback으로 손실 없이 매핑
- 두 독립 build가 contract/file/chunk/batch/coverage/selection에서 byte-identical
- 독립 verifier가 Git tree/OID/content, ranges, hashes, ID, rendered payload,
  next-fit membership, selection ranking, Experiment binding을 재계산해 PASS
- Positive/negative Phase 2 contract test 23개 PASS
- Frozen Experiment ID: `exp-v1-bea7f80c4b0484d6a706d8c7`

## Audit-driven corrections

초기 전체 corpus 산출물을 별도 Agent들이 적대적으로 감사한 뒤 다음 오류를 수정했다.

- `preproc_def`/`preproc_function_def`를 일반 PREPROCESSOR보다 먼저 분기해 MACRO로 분류
- Header guard와 namespace 같은 wrapper를 투명하게 내려가 내부 함수/타입을 복구
- 서로 다른 함수/타입/매크로/전역 declaration의 cross-unit 병합 금지
- 큰 함수의 자식 조각은 `AST_BLOCK`, 원 구조는 `parent_kind=FUNCTION`으로 보존
- Syntax 오류 하나 때문에 파일 전체를 버리지 않고 diagnostic unit만 fallback
- Half-open end point와 실제 마지막 포함 줄을 분리해 Evidence의 `end_line` 보정
- Experiment Lock의 3,840 Evidence cap/counter와 Builder 설정을 실행 전 대조
- Freeze 가능성을 canonical write 전에 검증하고, 결과 존재 시 re-freeze 금지
- 독립 verifier에서 Tree-sitter grammar/score/outcome과 보호 구조 단위를 재파싱
