# AI SAST Report

## Raspberry Pi Userland 대상 Multi-Agent 정적 보안 분석

-   작성자: 서민주
-   분석 대상: `raspberrypi/userland`
-   고정 Commit: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
-   사용 모델: `qwen2.5-coder:7b-instruct-q4_K_M`
-   주요 기술: Python, Ollama, Tree-sitter C/C++

------------------------------------------------------------------------

## 1. 프로젝트 개요

### 1.1 목표

본 프로젝트의 목표는 대규모 C/C++ 코드베이스를 LLM이 분석할 수 있는
단위로 분할하고, 여러 AI Agent가 역할을 나누어 정적 보안 분석을 수행하는
SAST 파이프라인을 구현하는 것이다.

분석 대상은 Raspberry Pi의 오래된 Userland 코드베이스이다. 대규모
Repository 전체를 한 번에 LLM Context에 입력할 수 없기 때문에 다음 세
가지 문제를 해결하는 데 초점을 두었다.

1.  대규모 소스코드를 코드 구조를 최대한 유지하면서 분할할 것
2.  특정 취약점 Rule에 해당하는 코드만 남기지 않고 전체 코드에 분석
    기회를 부여할 것
3.  여러 Agent가 동일한 코드를 반복해서 전달받으면서 발생하는 Context
    낭비를 줄일 것

최종적으로 Repository를 **26,645개 Chunk와 2,055개 Batch**로 구성하고,
Analyst, Context, Verifier의 3개 Agent가 연계되는 분석 구조를
구현하였다.

### 1.2 분석 범위

본 프로젝트는 소스코드를 실행하지 않는 정적 분석을 대상으로 한다. 함수,
타입, 호출 관계, include 관계와 실제 소스코드를 이용하여 취약점 후보를
찾는다.

최종 실험에서는 전체 2,055개 Batch 중 Priority가 높은 3개 Batch를 실제
LLM 분석 대상으로 사용하였다. 따라서 최종 실험 결과는 전체 Repository에
대한 취약점 유무를 의미하지 않는다.

------------------------------------------------------------------------

## 2. 전체 분석 구조

``` text
Raspberry Pi Userland
        |
        v
소스 파일 수집
        |
        v
Structural Chunking
        |
        +------> 26,645 Chunks
        |
        v
Batch 구성
        |
        +------> 2,055 Batches
        |
        +------> Compact Code Index
        |
        v
Priority 계산
        |
        v
Analyst Agent
   |             |
 CLEAR      추가 문맥 필요 / 취약점 후보
                 |
                 v
           Context Agent
                 |
                 v
          관련 코드 추가 조회
                 |
                 v
           Verifier Agent
                 |
        +--------+---------+
        |        |         |
    VERIFIED  REJECTED  INCONCLUSIVE
```

이 구조에서 **Priority는 분석 대상을 제거하는 필터가 아니다.** 전체
Batch를 분석 가능한 상태로 유지하면서 어떤 Batch를 먼저 분석할 것인지
결정하는 순서이다.

------------------------------------------------------------------------

## 3. Chunk와 Batch 설계

### 3.1 Chunk

Chunk는 정적 분석에서 사용하는 최소 코드 단위이다.

파일을 단순히 일정 글자 수나 Token 수로 자르면 함수가 중간에서
분리되거나 선언과 구현이 떨어질 수 있다. 이를 줄이기 위해 Tree-sitter
기반 C/C++ 파싱 결과를 이용하여 함수와 코드 구조를 최대한 보존하도록
분할하였다.

파싱이 정상적으로 수행되지 않는 파일도 분석 대상에서 제외하지 않고
Fallback 방식으로 Chunk를 생성하였다.

최종 결과는 다음과 같다.

  항목                            결과
  ------------------ -----------------
  대상 파일                        654
  Source 크기          8,594,578 bytes
  생성 Chunk                    26,645
  Parse success              403 files
  Fallback success           251 files
  Terminal error                     0

### 3.2 Batch

Batch는 Analyst Agent가 한 번의 분석 요청에서 전달받는 Chunk의 묶음이다.

Chunk 하나만 분석하면 함수 주변의 관련 코드가 부족할 수 있고, 너무 많은
Chunk를 묶으면 모델 Context 한도를 초과할 수 있다. 따라서 여러 Chunk를
Context 한도 안에서 하나의 Batch로 구성하였다.

최종적으로 **26,645개 Chunk를 2,055개 Batch로 구성**하였다.

Chunk와 Batch의 관계는 다음과 같다.

``` text
Source File
   ↓
Function / Structural Unit
   ↓
Chunk
   ↓
여러 Chunk를 Context 크기에 맞게 결합
   ↓
Batch
   ↓
Analyst Agent의 1회 분석 입력
```

------------------------------------------------------------------------

## 4. Compact Code Index

대규모 Repository에서 Analyst가 추가 문맥을 요구할 때마다 전체
소스코드를 다시 검색하거나 전달하면 비용이 커진다. 이를 위해 코드 탐색에
필요한 정보를 미리 Index로 구축하였다.

Index에는 다음 정보가 포함된다.

  항목              수량
  ------------- --------
  Files              654
  Symbols         19,620
  Calls           23,699
  Includes         2,773
  Chunk facts     26,645

중요한 점은 Index가 취약점 여부를 미리 판정하지 않는다는 것이다.

예를 들어 `strcpy`가 존재한다는 사실은 저장할 수 있지만, 해당 코드가
취약하다고 Index 단계에서 결정하지 않는다. 실제 보안 판단은 Analyst와
Verifier가 Raw Code를 확인한 뒤 수행한다.

이렇게 한 이유는 사전에 정의한 위험 함수나 Rule에 맞는 코드만 분석
대상으로 남길 경우, 입력 검증 오류나 상태 관리 문제처럼 단순 Pattern
Matching으로 찾기 어려운 취약점의 분석 기회가 줄어들 수 있기 때문이다.

------------------------------------------------------------------------

## 5. Batch Priority

전체 2,055개 Batch를 LLM으로 순차 분석하면 로컬 7B 모델 환경에서는 실행
시간이 지나치게 길어진다. 따라서 전체 Batch를 생성한 뒤 정적 정보를
이용해 분석 순서를 결정하는 Priority 기능을 추가하였다.

여기서 Priority의 역할은 다음과 같다.

``` text
잘못된 구조
Rule 불일치 Batch → 분석 대상에서 제외

본 프로젝트
전체 Batch 생성 → Priority 계산 → 높은 Batch부터 분석
```

즉 Priority가 낮더라도 Batch 자체가 삭제되거나 분석 불가능한 상태가 되지
않는다. 충분한 실행 시간이 있다면 전체 Batch를 순서대로 분석할 수 있다.

최종 실험에서는 제한된 시간 내 실제 Multi-Agent 동작을 검증하기 위해
Priority 상위 3개 Batch를 사용하였다.

------------------------------------------------------------------------

## 6. Multi-Agent 분석

### 6.1 Analyst Agent

Analyst는 실제 Raw Code를 읽고 취약점 후보를 찾는 1차 분석 Agent이다.

다음 항목을 중심으로 코드를 검토하도록 구성하였다.

-   Memory 및 Buffer Size
-   Integer/Size Conversion
-   Input Validation
-   Lifetime 및 Resource 관리
-   File/Path 처리
-   Format/String 처리
-   API Return 및 Error Handling
-   State 및 Authorization Logic

분석 결과는 크게 `CLEAR`, `CANDIDATE`, `NEED_CONTEXT` 형태로 처리된다.

`CLEAR`는 단순히 위험 함수가 보이지 않는다는 의미가 아니다. 주요 보안
관점을 검토했지만 현재 Batch에서 구체적인 취약점 후보를 찾지 못한 경우에
사용한다.

반대로 구체적인 의심점이 있지만 현재 Batch만으로 판단할 수 없다면
`NEED_CONTEXT`를 사용한다.

### 6.2 Context Agent

Context Agent는 취약점을 직접 판정하는 Agent가 아니다.

Analyst가 특정 함수, 타입, Macro, Caller, Callee 또는 상태 정보를 추가로
요구하면 Compact Code Index를 이용하여 관련 Chunk를 찾는다.

예를 들어 최종 실행에서는 다음과 같은 추가 문맥 요청이 발생하였다.

``` text
PATH
malloc
fread
fclose
glGenTextures
glBindTexture
glTexImage2D
sprintf_dup
sscanf
atoi
scandir
free
```

이 방식은 매번 Repository 전체나 원본 Batch 전체를 다시 전달하는 대신,
현재 판단에 필요한 코드만 추가로 전달하기 위한 것이다.

### 6.3 Verifier Agent

Verifier는 Analyst가 만든 보안 후보를 독립적으로 다시 확인한다.

Analyst의 판단을 그대로 받아들이지 않고 실제 전달된 Evidence를 기준으로
다음 중 하나를 결정한다.

-   `VERIFIED`: 전달된 코드만으로 실제 보안 문제가 뒷받침됨
-   `REJECTED`: 추가 확인 결과 취약점으로 보기 어려움
-   `INCONCLUSIVE`: 특정 정보가 부족하여 확정 또는 기각하기 어려움

최종 구현에서는 Analyst가 생성한 non-CLEAR 후보가 Context 조회 결과
때문에 검증 단계에서 누락되지 않도록 Verifier까지 전달되도록 구성하였다.

------------------------------------------------------------------------

## 7. Context 전달량 절감

### 7.1 문제

Multi-Agent 구조에서는 같은 코드를 Agent마다 반복해서 전달하면 Context
사용량이 빠르게 증가한다.

예를 들어 다음과 같은 구조는 비효율적이다.

``` text
Analyst  ← 전체 Batch
Context  ← 전체 Batch + Analyst 결과
Verifier ← 전체 Batch + Analyst 결과 + Context 결과
```

### 7.2 적용 방식

본 프로젝트에서는 Agent 사이에서 가능한 한 Finding ID, Chunk ID, 요청
Symbol 등의 참조 정보를 사용하고, 실제 코드가 필요할 때 관련 Chunk만
조회하도록 구성하였다.

``` text
Analyst
  ↓ Finding + 필요한 Symbol
Context Agent
  ↓ 관련 Chunk 선택
Verifier
  ↓ 선택된 Evidence 중심으로 검증
최종 판정
```

즉 **분석 코드 자체를 줄이는 것이 아니라 Agent 사이에서 동일한 Raw
Code가 반복 전달되는 양을 줄이는 방식**이다.

### 7.3 최종 측정 결과

  측정 항목                                           결과
  ---------------------------------------- ---------------
  전체 Batch 반복 전달을 가정한 Baseline     310,640 bytes
  실제 Evidence 전달량                        95,241 bytes
  절감량                                     215,399 bytes
  감소율                                        **69.34%**

최종 실험에서 Agent 간 Evidence 전달량은 Baseline 대비 **69.34%
감소**하였다.

이 수치는 모델 전체 Token 사용량이 69.34% 감소했다는 의미가 아니다. 본
프로젝트에서 별도로 측정한 **Agent 간 Evidence payload의 UTF-8 byte
크기**를 기준으로 한 결과이다.

------------------------------------------------------------------------

## 8. 재현성 확보

실험 과정에서 분석 대상과 모델 조건이 달라지면 결과를 비교하기 어렵다.
이를 줄이기 위해 Target과 Experiment 정보를 고정하였다.

### Target

-   Commit SHA: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
-   Target files: 654
-   Source bytes: 8,594,578

### Model

-   `qwen2.5-coder:7b-instruct-q4_K_M`
-   Ollama 사용
-   Context size: 16,384

### Experiment / Index

Phase별 Artifact를 생성하고 Hash를 기록하였다. Compact Code Index는 동일
조건으로 2회 구축하여 **byte-identical** 여부를 확인했으며 최종 결과는
`VERIFIED`였다.

이를 통해 최소한 동일 Target과 동일 구축 조건에서 Index Artifact가
재현되는지 확인하였다.

------------------------------------------------------------------------

## 9. 최종 실험

### 9.1 분석 대상

Priority 상위 3개 Batch를 실제 분석하였다.

1.  `B1-f3f830435063a72cf67c60c7`
2.  `B1-f94fe463bc695aff357c5c65`
3.  `B1-0bc83af29734fe4a66e43591`

### 9.2 Agent 실행 결과

  항목                                    결과
  -------------------- -----------------------
  분석 Batch                                 3
  Analyst 호출                               3
  Context Agent 호출                        21
  Verifier 호출                             21
  1차 Finding 후보                          21
  VERIFIED                                   0
  REJECTED                                  21
  최종 Batch 상태        NO_VERIFIED_FINDING 3

실제 실행에서는 Analyst가 21개의 후보를 생성하였다. 각 후보에 대해
Context Agent가 필요한 문맥을 조회했고 Verifier도 21회 호출되었다.

최종적으로 21개 후보는 모두 `REJECTED`되었으며 이번 실험에서 `VERIFIED`
취약점은 나오지 않았다.

### 9.3 실행량

  항목                                         결과
  --------------------------------------- ---------
  total_prompt_eval_count                    49,421
  total_eval_count                            6,772
  Analyst output enumeration truncation     1 Batch
  Raw Batch evidence silent truncation         없음

`truncated_batch_count=1`은 입력 Batch의 Raw Code가 잘렸다는 뜻이
아니다. Analyst가 제한된 출력 공간에서 Finding을 모두 열거하지 못했음을
의미한다.

------------------------------------------------------------------------

## 10. 결과 해석

이번 결과에서 가장 중요한 점은 `VERIFIED=0`을 잘못 해석하지 않는 것이다.

분석 대상 Repository가 안전하다고 결론 내릴 수 없다. 전체 2,055개 Batch
중 실제 LLM 분석을 수행한 것은 Priority 상위 3개뿐이기 때문이다.

또한 본 실험에서는 알려진 취약점 위치를 Ground Truth로 지정한 뒤 탐지
여부를 평가하지 않았다. 따라서 현재 결과만으로 취약점 탐지율이나
Recall을 주장할 수 없다.

반면 다음 기능은 실제 실행을 통해 확인하였다.

-   대규모 Repository 전체에 대한 Chunk/Batch 생성
-   654개 파일에 대한 Index 구축
-   전체 Batch에 대한 Priority 산정
-   Analyst의 Raw Code 분석
-   Analyst 요청에 따른 Context 탐색
-   Context 결과를 이용한 Evidence 전달
-   Verifier의 독립 검증
-   Agent별 실행 Telemetry 수집
-   Agent 간 Evidence 전달량 측정

따라서 이번 실험은 **취약점 탐지 성능을 입증한 실험이라기보다 설계한
Multi-Agent SAST 파이프라인이 실제 대규모 코드베이스에서 동작하는지를
확인한 구현 실험**으로 보는 것이 정확하다.

------------------------------------------------------------------------

## 11. 기존 접근과의 차이

본 프로젝트에서 강조할 부분은 새로운 취약점 탐지 알고리즘을 만들었다는
것이 아니다.

대규모 코드베이스를 LLM 기반 Multi-Agent SAST로 분석할 때 발생하는
**코드 분할, 분석 순서, 추가 문맥 탐색, Agent 간 Context 중복 전달**
문제를 하나의 파이프라인으로 구성한 것이 핵심이다.

  --------------------------------------------------------------------------------
  구분              단순 LLM 분석     Rule 기반 선별 후 본 프로젝트
                                      LLM               
  ----------------- ----------------- ----------------- --------------------------
  대규모 코드 처리  한 번에 입력하기  Rule로 분석량     Chunk/Batch로 분할
                    어려움            축소              

  분석 대상 결정    사용자가 코드     Rule 일치 코드    전체 Batch 유지
                    지정              중심              

  분석 순서         별도 기준 없음    Rule 결과에 의존  Priority로 순서 결정

  추가 문맥         수동 또는 전체    구현에 따라 다름  Index 기반 Context 조회
                    재입력                              

  Agent 역할        단일 모델 중심    주로 후보 재판정  Analyst/Context/Verifier
                                                        분리

  중복 코드 전달    발생 가능         발생 가능         필요한 Evidence 중심 전달

  최종 검증         최초 판단에 의존  Rule 또는 LLM     독립 Verifier
  --------------------------------------------------------------------------------

특히 **Priority와 분석 대상 선별을 구분한 것**이 중요하다. Priority가
낮다는 이유로 코드를 제거하지 않기 때문에 특정 Rule로 사전에 정의하기
어려운 취약점도 이후 분석될 수 있는 구조를 유지한다.

------------------------------------------------------------------------

## 12. 한계

### 12.1 실제 취약점 탐지 성능을 입증하지 못함

최종 3개 Batch에서 21개 후보가 생성되었지만 모두 Verifier에서
REJECTED되었다. 따라서 이번 결과만으로 실제 취약점 탐지 능력이
충분하다고 주장할 수 없다.

### 12.2 전체 Batch 정밀 분석 미수행

전체 2,055개 Batch를 생성했지만 시간과 로컬 모델 실행 비용 때문에 최종
실험에서는 3개만 LLM으로 분석하였다.

즉 시스템은 전체 Batch를 순회할 수 있도록 구성되어 있지만, 최종 제출
실험은 전체 분석 완료 결과가 아니다.

### 12.3 Ground Truth 평가 부재

Raspberry Pi Userland의 Known CVE와 실제 취약 코드 위치를 정답 데이터로
구축하여 탐지 성공 여부를 비교하지 못했다.

따라서 Precision, Recall, False Positive Rate와 같은 취약점 탐지 성능
지표는 제시하지 않는다.

### 12.4 로컬 모델의 한계

실험에는 Qwen2.5-Coder 7B 양자화 모델을 사용하였다. 복잡한
Inter-procedural Data Flow나 긴 코드 문맥 판단에서는 더 큰 Code LLM과
성능 차이가 발생할 수 있다.

### 12.5 정적 분석 자체의 한계

Runtime 환경, 실제 외부 입력, Thread Scheduling, 외부 Library 내부 동작
등에 강하게 의존하는 문제는 소스코드 정적 분석만으로 확정하기 어렵다.

------------------------------------------------------------------------

## 13. 향후 개선 방향

가장 먼저 필요한 개선은 기능을 더 추가하는 것이 아니라 **Known
Vulnerability 기반 탐지 성능 검증**이다.

Raspberry Pi Userland의 알려진 CVE와 취약 코드 위치를 Ground Truth로
만든 뒤 다음과 같이 평가할 수 있다.

``` text
Known Vulnerability
        ↓
취약 코드가 포함된 Chunk 확인
        ↓
해당 Chunk가 포함된 Batch 확인
        ↓
Priority 순위 확인
        ↓
Analyst 탐지 여부
        ↓
Context 탐색 적절성
        ↓
Verifier 최종 판정
```

이를 통해 다음 지표를 측정할 수 있다.

-   Known CVE Recall
-   Candidate Precision
-   Verifier False Rejection
-   Priority별 취약 Batch 분포
-   Batch 크기에 따른 탐지 성능
-   모델별 탐지 성능 및 비용

추가로 Call Graph와 Def-Use 관계를 강화하여 함수 간 Data Flow를 더
정확하게 연결하고, 동일 Root Cause에서 파생된 Finding을 하나로 묶는
기능도 개선할 수 있다.

------------------------------------------------------------------------

## 14. 결론

본 프로젝트에서는 Raspberry Pi Userland의 **654개 파일, 약 8.59 MB의
소스코드**를 대상으로 LLM 기반 Multi-Agent 정적 보안 분석 파이프라인을
구현하였다.

전체 소스코드를 **26,645개 Chunk와 2,055개 Batch**로 구성하고, 코드
탐색을 위한 Compact Code Index와 분석 순서를 결정하는 Priority 기능을
구축하였다.

실제 분석은 Priority 상위 3개 Batch를 대상으로 수행하였다. Analyst는
21개의 보안 후보를 생성했고, Context Agent가 21회 관련 문맥을 조회했으며
Verifier가 21개 후보를 독립 검증하였다. 최종적으로 21개 후보는 모두
REJECTED되어 이번 제한 실험에서는 VERIFIED 취약점이 확인되지 않았다.

따라서 본 결과를 실제 취약점 탐지 성능의 입증으로 해석하지 않는다. 대신
**대규모 C/C++ Repository를 구조적으로 분할하고, 필요한 코드 문맥을
추가로 탐색하며, 독립 검증까지 수행하는 Multi-Agent SAST 파이프라인을
실제로 구현하고 실행했다는 점**에 의미가 있다.

또한 Agent 간 동일 Raw Code의 반복 전달을 줄이고 필요한 Evidence를
중심으로 전달하여, 최종 실험에서 측정한 Agent 간 Evidence payload를
Baseline **310,640 bytes에서 95,241 bytes로 줄였다. 이는 69.34% 감소한
결과**이다.

향후 Known CVE 기반 Ground Truth 평가를 추가하면 현재 구현한
파이프라인의 실제 취약점 탐지 성능과 Priority 전략의 효과를 정량적으로
검증할 수 있다.
