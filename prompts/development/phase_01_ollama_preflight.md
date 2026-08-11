# Phase 1 — OllamaProvider Preflight and Experiment Lock

## User prompt

```text
알아서 해줘 그리고 Phase 단계가 하나 끝날 때마다 github에 커밋도 같이 해줄래
```

## Operational interpretation

Canonical Phase를 완료 기준에 따라 순서대로 구현한다. 각 Phase는 positive/negative
검증이 모두 통과한 뒤 그 Phase의 변경만 Git commit으로 고정한다. 현재 프로젝트에
Git remote가 없으므로 Phase별 local commit을 먼저 만들고, remote가 연결되면 해당
commit들을 push한다.

## Phase-specific implementation prompt

```text
v1 Runtime Backend를 Ollama로 고정한다. 설치된 모델의 tag가 아니라 /api/tags의
full immutable manifest digest를 잠그고, /api/show의 native context, template,
tokenizer metadata를 검증한다. 실제 /api/chat 경로에서 고정 seed와 JSON Schema를
사용한 structured-output smoke를 두 번 실행한다. System/User/Evidence/Tool Schema/
Structured State/Chat Template/Reserved Output/Safety Margin을 모두 포함하는 hard
context envelope를 만들고 고수위 실제 prompt로 검증한다. Target lock과 Runtime,
token counting, retry/timeout, Baseline/Proposed 공통 조건, 결과 독립 3-Batch 선택
계약을 self-reference 없는 semantic hash로 experiment.lock.yaml에 봉인한다.
```

## Decisions made

- Runtime backend: Ollama `0.32.8`, local API `http://127.0.0.1:11434`
- Model: `qwen2.5-coder:7b-instruct-q4_K_M`
- Full model manifest SHA-256:
  `dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364`
- Model native context: 32,768; experiment `num_ctx`: 8,192
- Inference: temperature 0, seed 20260811, `num_predict` 1,280,
  `stream=false`, concurrency 1
- Provider counters: input `prompt_eval_count`, output `eval_count`
- Phase 2 Batch budgeting: deterministic UTF-8 byte upper bound; actual totals remain
  provider counters
- Experiment identity: canonical `semantic` object only를 domain-separated SHA-256한
  `exp-v1-<24 hex>`; timestamp/latency/artifact path는 identity에 영향 없음
- Phase 1에서는 Batch ID를 만들지 않고 count=3, seed, ranking, selection path,
  result-based replacement 금지만 잠금. Phase 2에서 실제 selection binding을 추가하면
  semantic 변경에 따라 새 Experiment ID를 발급

## Verification result

- Ollama version, installed model, full digest, model metadata 검증 통과
- 동일 seed structured JSON `/api/chat` 2회 결과 일치
- 실제 loaded context 8,192, CPU execution 확인
- Chat-template/System/User/Evidence/Structured-State/Tool+Response-Schema의 6단계
  누적 prefix-differential 측정 통과
- Context input cap 6,144 중 실제 high-water probe 5,392 tokens(87.76%) 통과;
  component별 delta가 모두 개별 cap 안에 있고 합계가 provider total과 일치
- 독립 offline verifier, live verifier 및 6개 prefix 전체 `--live-full` 재실행 통과
- Semantic identity, artifact drift, retry/timeout, locked request gate, context overflow,
  response-schema enforcement, Phase 2 stage transition을 포함한 positive/negative
  test 26개 통과
- Experiment ID: `exp-v1-66575973a148987d741ba3f9`
- Semantic SHA-256:
  `66575973a148987d741ba3f95fc492fd141e4a30b13ce7262d054f0d02209ba7`
