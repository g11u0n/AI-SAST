# AI SAST Report

## Raspberry Pi Userland 대상 Multi-Agent 정적 보안 분석

-   작성자: 서민주
-   대상: `raspberrypi/userland`
-   고정 Commit: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
-   모델: `qwen2.5-coder:7b-instruct-q4_K_M`
-   실행 환경: Python, Ollama, Tree-sitter C/C++

------------------------------------------------------------------------

## 1. 과제 목표

본 과제는 대규모 C/C++ Repository를 효과적으로 분할하고, 분할된 코드 중
3개 Batch를 실제로 분석하며, 역할이 분리된 Multi-Agent 구조와 Token 절약
방안을 구현하는 것을 목표로 한다.

본 구현은 위험 API가 존재하는 코드만 선별하여 LLM에게 전달하는 방식이
아니다. Repository 전체를 구조적으로 Chunk로 분리하고, Chunk를 모델
Context 한도에 맞는 Batch로 구성한다. Analyst가 Batch를 직접 분석하고,
추가 문맥이 필요한 경우에만 Context Agent가 관련 코드를 조회하며, 최종
후보는 Verifier가 독립적으로 재검증한다.

## 2. 핵심 설계 원칙

### 2.1 전체 코드 분석 대상 유지

특정 위험 API의 존재 여부를 분석 대상 포함/제외 기준으로 사용하지
않았다. Memory Safety뿐 아니라 Input Validation, Error Handling,
State/Authorization Logic 등 패턴만으로 잡기 어려운 문제도 분석 기회를
유지하기 위함이다.

### 2.2 Chunk와 Batch 분리

-   **Chunk**: 함수, 타입 정의, 전역 코드 등 구조적으로 분리한 최소 코드
    단위
-   **Batch**: Analyst가 한 번의 1차 분석에서 받는 여러 Chunk의 묶음

Tree-sitter를 이용해 함수와 의미 단위를 최대한 보존했다. 최종적으로
654개 파일에서 **26,645개 Chunk**를 생성하고 이를 **2,055개 Batch**로
구성했다.

### 2.3 사실과 보안 판단 분리

Compact Code Index에는 symbol, signature, call/include relation, AST
fact 등 기계적으로 추출 가능한 사실만 저장한다. Index 단계에서 `취약함`,
`Buffer Overflow 가능성` 같은 보안 판단은 하지 않는다.

## 3. 전체 구조

``` text
Raspberry Pi Userland
        |
        v
Structural Chunker
        |
        +----> 26,645 Chunks ----> Batch Generator ----> 2,055 Batches
        |
        +----> Compact Code Index
                                  |
                                  v
                           Priority Ranking
                                  |
                                  v
                            Analyst Agent
                         /                 \
                      CLEAR        NEED_CONTEXT/CANDIDATE
                                           |
                                           v
                                     Context Agent
                                           |
                                  On-Demand Evidence
                                           |
                                           v
                                     Verifier Agent
                                           |
                         VERIFIED / REJECTED / INCONCLUSIVE
```

Priority Ranking은 Batch를 제거하는 필터가 아니라 전체 Batch의 **분석
순서**를 결정하는 Scheduler다.

## 4. Agent 구성

### Analyst Agent

Batch의 Raw Code를 읽고 Memory/Bounds, Integer/Size, Input Validation,
Lifetime/Resource, API/Error Handling, Path/Format, State/Authorization
Logic 등을 포괄적으로 검토한다. 정보가 부족하면 추측하지 않고
`NEED_CONTEXT`를 사용한다. `CLEAR`는 memory/size, validation,
lifetime/resource, API/state를 검토한 뒤에만 허용하도록 Prompt와
Structured Output을 구성했다.

### Context Agent

Analyst가 요청한 symbol, caller, callee, macro, type, state와 관련된
Chunk를 Compact Code Index에서 탐색한다. Repository 전체 Raw Source를
재전달하지 않고 판단에 필요한 Evidence만 선택한다. Context Agent 자체는
취약점 최종 판정을 수행하지 않는다.

### Verifier Agent

Analyst의 후보를 정답으로 전제하지 않고 Evidence를 기준으로 독립
검증한다. 최종 결과는 `VERIFIED`, `REJECTED`, `INCONCLUSIVE` 중 하나로
반환한다. 최종 버전에서는 보안 후보가 Context 검색 결과에 따라 검증
단계에서 누락되지 않도록 모든 non-CLEAR 후보를 독립 검증한다.

## 5. Token 절약 전략

핵심 설계는 **Reference-Only Multi-Agent Handoff + On-Demand Evidence
Pull**이다.

일반적인 구조에서는 동일 Raw Batch가 Agent가 바뀔 때마다 반복 전달될 수
있다. 본 구현은 Agent 사이에서 Finding, Chunk ID, Context Request 같은
Reference를 우선 전달하고, 필요한 Agent만 필요한 Raw Chunk를 다시
조회한다.

> **Code once, reference thereafter.**

적용한 방법은 다음과 같다.

1.  Batch 기반 순차 분석
2.  Reference-Only Handoff
3.  On-Demand Evidence Pull
4.  Compact Code Index 기반 탐색
5.  생성된 Chunk/Index/Relation Artifact 재사용
6.  Structured Output 기반 Agent 통신

## 6. 구축 결과

### Target Coverage

-   대상 파일: **654**
-   Source bytes: **8,594,578**
-   Path exclusion: 없음
-   Terminal error: **0**

### Structural Chunking

-   Chunk: **26,645**
-   Batch: **2,055**
-   Parse success: **403 files**
-   Fallback success: **251 files**

### Compact Code Index

-   Files: **654**
-   Symbols: **19,620**
-   Calls: **23,699**
-   Includes: **2,773**
-   Chunk facts: **26,645**
-   Terminal errors: **0**
-   Reproducibility: **VERIFIED**

Index는 동일 입력에 대해 2회 구축 결과가 byte-identical함을 확인했다.

## 7. 실제 분석 Batch

최종 과제 실험은 Priority 상위 3개 Batch를 대상으로 수행했다.

1.  `B1-f3f830435063a72cf67c60c7`
2.  `B1-f94fe463bc695aff357c5c65`
3.  `B1-0bc83af29734fe4a66e43591`

## 8. 최종 실험 결과

  항목                                    결과
  -------------------- -----------------------
  실제 분석 Batch                            3
  Analyst 호출                               3
  Context Agent 호출                        21
  Verifier 호출                             21
  Finding 후보                              21
  VERIFIED                                   0
  REJECTED                                  21
  최종 Batch 상태        NO_VERIFIED_FINDING 3

Analyst는 총 21개의 보안 후보를 생성했고, 각 후보에 대해 Context 탐색과
독립 Verifier 호출이 수행됐다. Verifier는 21개 후보를 모두 `REJECTED`
처리했으며 이번 3-Batch 제한 실험에서 최종 `VERIFIED` 취약점은 확인되지
않았다.

이 결과는 **Raspberry Pi Userland에 취약점이 존재하지 않는다는 의미가
아니다.** 전체 2,055개 Batch 중 상위 3개만 LLM 정밀 분석한 제한
실험이므로 전체 Repository의 취약점 유무나 탐지율을 본 결과만으로 주장할
수 없다.

### Handoff 절감 효과

  측정값                      UTF-8 bytes
  ------------------------- -------------
  Baseline retransmission         310,640
  Proposed handoff                 95,241
  Avoided                         215,399
  Reduction                    **69.34%**

최종 실행에서 Reference 중심 Handoff와 필요한 Evidence만 Pull하는 구조를
통해 Agent 간 Evidence 전달량이 Baseline 대비 **69.34% 감소**했다.

### Model Telemetry

-   `total_prompt_eval_count`: **49,421**
-   `total_eval_count`: **6,772**
-   Analyst output enumeration truncation Batch: **1**
-   Raw Batch evidence silent truncation: **없음**

`truncated_batch_count=1`은 Raw Batch 입력이 잘렸다는 의미가 아니라
Analyst가 응답 공간에서 모든 Finding을 열거하지 못했음을 의미한다.

## 9. 구현 Prompt 작성 시 주안점

구현 과정에서는 AI에게 단순히 SAST 구현을 요청하지 않고 다음 Contract를
명시했다.

``` text
- Raspberry Pi Userland 전체 C/C++ 코드를 분석 대상으로 유지한다.
- 특정 위험 API가 있는 파일만 선별하지 않는다.
- 함수/구조 단위 Chunk를 만들고 Context 한도에 맞게 Batch화한다.
- Index에는 보안 판단이 아니라 기계적으로 추출한 사실만 저장한다.
- Analyst, Context, Verifier의 책임을 분리한다.
- 추가 문맥은 필요한 경우에만 Pull한다.
- Verifier는 Analyst의 판단을 독립적으로 검증한다.
- VERIFIED / REJECTED / INCONCLUSIVE 상태를 구분한다.
- Target commit, model digest, experiment artifact를 고정해 재현성을 확보한다.
```

Agent Prompt 역시 역할별로 분리했다. Analyst는 실제 취약점 후보 발굴,
Context Agent는 필요한 코드 탐색, Verifier는 독립 검증만 수행하게 하여
하나의 Agent가 탐색·판단·검증을 모두 수행하면서 발생할 수 있는
자기확증을 줄이고자 했다.

## 10. 차별점

  -----------------------------------------------------------------------
  구분              단순 LLM SAST     Rule/Pattern +    본 프로젝트
                                      LLM               
  ----------------- ----------------- ----------------- -----------------
  대형 Repository   Context 한계      Rule로 후보 축소  Structural
  처리                                                  Chunk + Batch

  분석 대상         입력된 코드       Rule hit 중심     전체 코드 대상
                                                        유지

  문맥 탐색         Raw Code 재전달   도구별 상이       Compact Code
                                                        Index

  Agent 간 전달     Raw Code 반복     결과 전달 중심    Reference +
                                                        Evidence Pull

  검증              단일 판단         Rule 결과 재판정  독립 Verifier

  Token 절약        입력 축소         후보 필터링       Workflow 중복
                                                        제거

  Logic             제한적            Rule 의존         취약점 유형을
  Vulnerability                                         사전 제한하지
  분석 기회                                             않음
  -----------------------------------------------------------------------

본 프로젝트는 Retrieval 자체를 새로운 기술이라고 주장하지 않는다.
차별점은 **대형 코드베이스 Multi-Agent SAST에서 반복되는 Raw Context를
Reference 중심으로 줄이고, 필요한 Evidence만 다시 조회하여 독립 검증
단계와 연결한 구조**에 있다.

## 11. 한계

1.  **VERIFIED 0건**: 최종 3-Batch 실험에서는 21개 후보가 모두
    REJECTED됐다. 실제 알려진 취약점에 대한 Recall/Precision은 입증하지
    못했다.
2.  **분석 범위 제한**: 2,055개 Batch 전체가 아닌 상위 3개만 LLM 정밀
    분석했다.
3.  **모델 성능 의존성**: 로컬 실행 제약으로 Qwen2.5-Coder 7B 양자화
    모델을 사용했다.
4.  **정적 분석의 한계**: Concurrency, Runtime 환경, 외부 시스템 상태에
    강하게 의존하는 문제는 핵심 범위가 아니다.
5.  **Priority와 취약점 존재는 별개**: 높은 Priority가 실제 취약점을
    포함한다는 보장은 없다.

## 12. 향후 개선

-   Raspberry Pi Userland Known CVE를 Ground Truth로 구축하여 Recall
    검증
-   Priority Top-K 확대 및 전체 Batch 순회 실험
-   대형 Code LLM과 Qwen2.5-Coder 7B 비교
-   caller/callee뿐 아니라 def-use/data-flow relation 강화
-   중복 Finding 및 동일 Root Cause clustering
-   CWE, severity, exploit precondition 후처리
-   wall-clock time과 실제 Token counter를 포함한 비용/성능 평가

## 13. 결론

본 프로젝트는 Raspberry Pi Userland의 654개 C/C++ 파일을 대상으로
26,645개의 구조적 Chunk와 2,055개의 Batch를 생성하고 Compact Code Index
및 Priority Scheduler를 구축했다. 이후 Analyst, Context, Verifier로
역할을 분리한 Multi-Agent 정적 분석 파이프라인을 실제 상위 3개 Batch에
적용했다.

최종 실험에서 Analyst는 21개의 후보를 생성했고 Context Agent와
Verifier가 각각 21회 호출됐다. 모든 후보가 최종 REJECTED되어 이번 제한
실험에서 VERIFIED 취약점은 확인하지 못했다. 따라서 탐지 성능을 과장하지
않는다.

반면 Multi-Agent 파이프라인 자체는 실제 동작했으며, Reference-Only
Handoff와 On-Demand Evidence Pull을 통해 Baseline 대비 Agent 간 Evidence
전달량을 **69.34% 감소**시켰다. 본 구현의 핵심 성과는 대규모
코드베이스에서 전체 코드의 분석 가능성을 유지하면서 Multi-Agent 간 중복
Context 전달을 줄이고 필요한 Evidence만 조회하여 독립 검증까지 연결한
것이다.
