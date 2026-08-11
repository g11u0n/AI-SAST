# AI SAST 과제 개발 명세서
## Raspberry Pi Userland 대상 Multi-Agent SAST

> 이 문서는 Codex에게 개발 맥락과 요구사항을 전달하기 위한 프로젝트 명세서다.
> 목표는 “논문 수준의 복잡한 연구 시스템”이 아니라, 과제 요구사항을 빠르게 충족하면서도 구조적 차별성과 실제 동작 결과를 보여줄 수 있는 구현 가능한 AI SAST를 만드는 것이다.

---

# 1. 과제 원문 요구사항 요약

## Target
- 대상 코드: 오래된 Raspberry Pi Userland 애플리케이션
- Repository: https://github.com/raspberrypi/userland

## 반드시 포함해야 하는 조건
1. 거대한 Repository를 효과적으로 탐색하기 위한 **코드 분할 처리 기능**
2. 분할된 코드 Batch 중 **3개 Batch에 대한 실제 분석 결과**
3. **Multi-Agent 환경**으로 구성하고 각 Agent에 명확한 Role 부여
4. **Token 절약 방안** 수립

## 제출 산출물
- 작성한 도구의 GitHub Repository URL
- 각 Agent의 구성도
- 각 Agent의 Skill 작성 시 주안점
- Token 절약을 위해 도입한 설계와 그 이유
- 구현 과정에서 AI에게 사용한 Prompt
- 다른 도구와 비교했을 때의 차별점
- 최종 Report (`.md` 또는 `.html`)

## 산출물 계약

최종 제출물은 이름만 언급하지 않고 아래 파일과 검증 증거로 연결한다.

- `SUBMISSION.md`: Target URL/Commit, 제출 Commit의 full SHA, 재현 명령,
  모든 산출물 링크를 모은 제출 인덱스
- `docs/architecture.md`: Agent, Tool, Handoff, deterministic/LLM 경계,
  Telemetry를 표시한 렌더 가능한 Mermaid 구성도
- `docs/agent_skills.md`: 세 Agent별 Role, 입력/출력, Tool, 허용/금지 행동,
  Prompt 주안점, 종료/실패 조건
- `results/batch_*/`: 실제 3개 Batch 결과와 schema-valid trace/telemetry
- `results/external_sast/<tool>/`: 최소 1개 외부 SAST의 version, command,
  config/ruleset, raw/normalized output, 동일 범위 비교표
- `report/report.md`: 구조, Skill, Prompt, 3-Batch 결과, 내부 Token baseline,
  외부 도구 비교, 한계, 재현 절차와 raw artifact 링크
- `presentation/slides.pdf`, `presentation/demo_script.md`,
  `presentation/rehearsal_checklist.md`: 발표·Demo와 rehearsal 증거

완료 시 placeholder가 없어야 하며 모든 링크와 수치는 실제 파일 및 raw evidence로
역추적 가능해야 한다. GitHub URL과 제출 Commit full SHA는 Phase 16 게시 후 확정한다.

## 평가 기준
1. 신뢰도와 효율성 측면에서 설득력 있는 구조
2. 흔하지 않고 차별성 있는 가치를 가진 창의적인 구조
3. 구현 아이디어를 AI에게 효과적으로 Prompt화한 방법
4. 설득력 있는 발표 구성

---

# 2. 프로젝트 핵심 목표

이 프로젝트는 단순히 기존 Rule-Based SAST의 후보를 LLM이 재판정하는 도구가 아니다.

목표는 다음과 같다.

> **대규모 C/C++ Repository를 정적으로 분할한 뒤, 각 Batch를 AI가 취약점 유형에 제한되지 않고 포괄적으로 분석하고, 추가 코드 문맥이 필요한 경우에만 Repository에서 관련 코드를 찾아오는 Multi-Agent SAST를 구현한다.**

중요 원칙:

1. **모든 in-scope 코드는 구조화 대상이다.**
   - `memcpy`, `malloc`, `system` 등 특정 위험 API가 있는 코드만 AI에게 보여주면 안 된다.
   - Logic Vulnerability처럼 단순 Pattern으로 잡히지 않는 문제도 AI가 분석할 기회를 가져야 한다.
   - 고정 Target의 모든 in-scope 파일을 Chunking·Indexing·Batching해 임의 Batch를
     분석할 수 있어야 한다. 과제의 실제 LLM 평가는 결과 확인 전에 고정한 3개
     Batch에 대해 수행한다.

2. **취약점 종류를 미리 좁히지 않는다.**
   - Memory Safety, Integer, Pointer/Lifetime, Input Validation, Format, Command/Path, Resource, Error Handling, State/Authorization Logic 등 정적으로 판단 가능한 문제를 포괄적으로 찾는다.
   - 특정 CWE 목록은 “참고 영역”일 뿐 탐지 범위 제한 조건이 아니다.

3. **정적으로 확정할 수 없는 것은 억지로 판정하지 않는다.**
   - `VERIFIED`
   - `REJECTED`
   - `INCONCLUSIVE`
   같은 상태를 사용한다.

4. **Concurrency 및 실행환경에 강하게 의존하는 Runtime-only 문제는 v1의 핵심 목표에서 제외한다.**
   - 다만 코드에서 명백한 정적 단서를 발견하면 Candidate로 언급할 수 있다.

---

# 3. 프로젝트를 처음 보는 사람을 위한 전체 구조

전체 흐름은 아래와 같다.

```text
Raspberry Pi Userland Repository
            |
            v
+---------------------------+
| 1. Structural Chunker     |
| - C/C++ 소스 파싱         |
| - 함수/의미 단위 분할     |
| - 큰 함수 추가 분할       |
+-------------+-------------+
              |
              v
       여러 Code Chunk
              |
              +------------------------------+
              |                              |
              v                              v
+---------------------------+     +---------------------------+
| Compact Code Index        |     | Batch Generator           |
| - symbol                  |     | - Chunk들을 Batch화       |
| - file/line               |     | - 모델 Context 한도 고려 |
| - calls/called_by         |     +-------------+-------------+
| - signature               |                   |
| - basic AST facts         |                   v
| - token count             |          Raw Code Batch
+-------------+-------------+                   |
              |                                 v
              |                     +---------------------------+
              |                     | Agent 1: Analyst          |
              |                     | - Batch 전체 보안 분석    |
              |                     | - 취약점 유형 제한 없음   |
              |                     | - 부족한 Context 요청     |
              |                     +-------------+-------------+
              |                                   |
              |                    NEED_CONTEXT / Candidate
              |                                   |
              v                                   v
+---------------------------+         +---------------------------+
| Agent 2: Context Agent    |<--------| Context Request           |
| - Index 검색              |         +---------------------------+
| - 필요한 symbol/caller/   |
|   callee/type/macro 탐색   |
| - 관련 Raw Chunk만 제공   |
+-------------+-------------+
              |
              v
      Analyst 추가 분석
              |
              v
       Candidate Finding
              |
              v
+---------------------------+
| Agent 3: Verifier         |
| - Claim을 그대로 믿지 않음|
| - Evidence Ref로 원본 재조회|
| - 독립적으로 재검증       |
+-------------+-------------+
              |
              v
 VERIFIED / REJECTED / INCONCLUSIVE
              |
              v
          Final Report
```

---

# 4. 코드 분할 설계

## 왜 분할하는가
Repository 전체를 한 번에 LLM Context에 넣는 방식은 대형 코드베이스에서 현실적이지 않다.

따라서 분석 전에 코드 자체를 작은 단위로 분리해야 한다.

## 기본 분할 단위
우선순위:

1. 함수(Function)
2. 전역 초기화 코드
3. Struct / Enum / Type Definition
4. Macro / Preprocessor 관련 필요한 정의
5. 함수가 지나치게 큰 경우 AST Block 단위 추가 분할

단순히 `1~500줄`, `501~1000줄`처럼 줄 번호로 자르면 함수가 중간에서 끊길 수 있으므로 피한다.

## Chunk Metadata 예시

```json
{
  "chunk_id": "C0184",
  "file": "src/example.c",
  "symbol": "process_data",
  "kind": "function",
  "start_line": 120,
  "end_line": 188,
  "token_count": 732
}
```

---

# 5. Batch의 의미

이 프로젝트에서는 `Chunk`와 `Batch`를 구분한다.

## Chunk
Repository를 구조적으로 분리한 최소 코드 단위.

## Batch
Analyst Agent가 한 번의 1차 분석에서 실제로 받는 여러 Chunk의 묶음.

예:

```text
Batch 01
- C0001
- C0002
- C0003
- ...
```

Batch 크기는 Repository 전체 크기에 대한 고정 비율로 정하지 않는다.

대신 설정 가능한 `max_batch_tokens` 또는 모델 Context 크기를 기준으로 동적으로 구성한다.

예:

```yaml
batch:
  max_batch_tokens: 12000
```

정확한 값은 구현 후 Target Repository와 사용하는 모델을 기준으로 조정한다.

---

# 6. Compact Code Index

이전 아이디어의 `Compact Security Index`라는 이름은 오해를 줄 수 있으므로 **Compact Code Index**로 부른다.

## 역할
Compact Code Index는 취약점을 판정하는 장치가 아니다.

목적은:

> **Analyst가 추가 문맥이 필요할 때 전체 Repository를 다시 읽지 않고, 필요한 코드를 빠르게 찾게 해주는 Navigation Index**

이다.

## Index에 넣을 수 있는 정보
가능하면 Tree-sitter 등으로 기계적으로 확실하게 추출 가능한 사실만 저장한다.

예:

```json
{
  "chunk_id": "C0184",
  "file": "src/example.c",
  "symbol": "process_data",
  "signature": "int process_data(char *buf, int len)",

  "calls": [
    "parse_header",
    "memcpy"
  ],

  "called_by": [
    "handle_packet"
  ],

  "parameters": [
    "char *buf",
    "int len"
  ],

  "pointer_ops": true,
  "array_access": false,
  "conditions": 3,
  "loops": 1,

  "token_count": 732
}
```

## Index에 넣으면 안 되는 정보
다음과 같은 AI 판단은 Index 생성 단계에 넣지 않는다.

```text
"Buffer Overflow 가능성 높음"
"UAF Candidate"
"취약함"
"안전함"
```

Index는 **사실(Fact)** 을 저장하고, 취약점 여부는 Agent가 판단한다.

---

# 7. Multi-Agent 구성

## Agent 1. Analyst Agent

### Role
각 Batch의 Raw Code를 실제로 읽고 포괄적인 보안 분석을 수행한다.

### 주요 원칙
- 특정 CWE 몇 개로 탐지 범위를 제한하지 않는다.
- Known SAST Pattern은 참고 힌트로 사용할 수 있지만 그것만 찾지 않는다.
- 논리적인 보안 문제도 분석한다.
- 없는 코드나 Symbol을 상상하지 않는다.
- 필요한 정보가 없으면 추측하지 않고 `NEED_CONTEXT`를 반환한다.

### 분석 영역 예시
아래는 최소 참고 영역이며, 탐지 범위를 이 목록으로 제한하지 않는다.

- Memory Safety
- Buffer / Bounds
- Integer Overflow / Size Calculation
- Pointer / NULL
- Allocation / Lifetime / UAF / Double Free
- Input Validation
- Format String
- Command / Path handling
- File / Resource Management
- Error Handling
- Hardcoded Secret
- Insecure API Misuse
- State Validation
- Authorization / Logic Vulnerability
- Trust Assumption / Validation Gap

### 출력 예시

```json
{
  "status": "NEED_CONTEXT",
  "reason": "caller-side length validation을 확인해야 함",
  "requests": [
    {
      "type": "callers",
      "symbol": "process_data"
    }
  ]
}
```

또는:

```json
{
  "status": "CANDIDATE",
  "title": "Potential out-of-bounds write",
  "description": "...",
  "evidence": [
    {
      "chunk_id": "C0184",
      "lines": "142-149"
    }
  ]
}
```

---

## Agent 2. Context Agent

### Role
Analyst가 요청한 추가 Context를 Repository 전체를 다시 읽지 않고 찾아주는 Agent.

### 사용하는 정보
- Compact Code Index
- Symbol Index
- Call relation
- File / Header relation
- Chunk Store

### 요청 예시

```json
{
  "type": "callers",
  "symbol": "process_data",
  "reason": "caller-side validation 확인"
}
```

### 응답 예시

```json
{
  "matched_chunks": [
    {
      "chunk_id": "C0031",
      "symbol": "handle_packet"
    },
    {
      "chunk_id": "C0812",
      "symbol": "dispatch_message"
    }
  ]
}
```

Context Agent는 가능하면 요청 목적에 가장 직접적인 코드부터 제공한다.

목표는 **한 번에 관련 코드를 전부 가져오는 것이 아니라 필요한 만큼만 단계적으로 가져오는 것**이다.

---

## Agent 3. Verifier Agent

### Role
Analyst의 Finding을 그대로 믿지 않고 독립적으로 검증한다.

### 입력
가능하면 Analyst의 장황한 전체 추론을 전달하지 않는다.

다음처럼 최소 정보만 전달한다.

```json
{
  "claim": "Potential out-of-bounds write",
  "evidence": [
    {
      "chunk_id": "C0184",
      "lines": "142-149"
    },
    {
      "chunk_id": "C0031",
      "lines": "81-94"
    }
  ]
}
```

### 검증 절차
1. Evidence Reference의 Raw Code를 다시 Pull
2. Claim과 실제 코드가 일치하는지 확인
3. Caller/Validation/Guard 등 반증 가능성 확인
4. 존재하지 않는 코드나 호출관계는 인정하지 않음
5. 아래 중 하나 반환

```text
VERIFIED
REJECTED
INCONCLUSIVE
```

---

# 8. 핵심 차별화 아이디어

## Reference-Only Multi-Agent Handoff + Evidence Pull

일반적인 단순 Multi-Agent 구현에서는 동일 Raw Code가 Agent마다 반복해서 Context에 들어갈 수 있다.

예:

```text
Raw Batch
  -> Agent A

Raw Batch + A 결과
  -> Agent B

Raw Batch + A 결과 + B 결과
  -> Agent C
```

이 프로젝트에서는 Agent 사이에 가능한 한 Raw Source를 직접 넘기지 않는다.

```text
Analyst
  -> Chunk ID / Evidence Ref / Context Request

Context Agent
  -> 요청된 Raw Chunk만 제공

Verifier
  -> Evidence Ref를 기반으로 필요한 Raw Code를 직접 재조회
```

핵심 문장:

> **Code once, reference thereafter.**

한국어 표현:

> **모든 Agent가 모든 코드를 반복해서 읽는 것이 아니라, 필요한 Agent가 필요한 증거만 원본에서 조회한다.**

주의:
- 이 구조를 “세계 최초”라고 주장하지 않는다.
- 선택적 Retrieval 자체는 기존에도 존재할 수 있다.
- 우리 과제에서의 차별점은 **대형 AI SAST의 Multi-Agent 간 Context 중복 문제를 Reference 중심 Handoff로 줄이고, Evidence 기반 검증과 연결한 구조**라고 설명한다.

---

# 9. Token 절약 전략

Repository 전체 Token 총량을 사전에 임의로 제한하지 않는다.

Token 절약은 **Agent Workflow 자체를 효율적으로 만드는 방식**으로 해결한다.

## 1. Batch 기반 순차 분석
전체 Repository를 한 번에 LLM에 전달하지 않는다.

## 2. Reference-Only Handoff
Agent 간 동일 Raw Code의 반복 전달을 최소화한다.

## 3. On-Demand Evidence Pull
추가 Context는 실제 판단에 필요한 경우에만 조회한다.

## 4. Compact Code Index
전체 Repository를 검색할 때 Raw Code 대신 작은 Index를 우선 사용한다.

## 5. Cache
이미 생성된:
- Chunk
- Index
- Symbol relation
- Retrieved Context
등은 재사용한다.

## 6. Structured Output
Agent 간 장문의 자연어 설명 대신 JSON 등 구조화된 짧은 결과를 전달한다.

---

# 10. Token 절약 효과 측정

Token 절약은 사전에 추정값을 만들어내기보다 **실제 3개 Batch 실행 후 측정한다.**

## Baseline
동일한 Raw Batch를 각 Agent에게 반복 전달하는 단순 Multi-Agent 구조.

예:

```text
Analyst  -> Raw Batch
Context  -> Raw Batch 또는 전체 관련 코드
Verifier -> Raw Batch + Finding
```

## Proposed
현재 프로젝트 구조.

```text
Analyst  -> Raw Batch
Context  -> Compact Index + 요청 정보
Verifier -> Evidence Ref + 필요한 Raw Code만 Pull
```

## 측정 항목
각 Batch에 대해 최소 다음을 기록한다.

```text
- Raw Batch Token
- Agent별 Input Token
- Agent별 Output Token
- 추가 Pull된 Chunk 수
- Pull된 추가 Token
- 전체 Token 사용량
- Finding 수
- VERIFIED 수
- REJECTED 수
- INCONCLUSIVE 수
```

최종 Report에서 Baseline 대비 실제 Token 감소량을 제시한다.

---

# 11. 3개 Batch 결과 요구사항

과제에서 반드시 3개 Batch의 실제 결과를 제시해야 한다.

각 Batch 결과에는 최소 다음이 포함되어야 한다.

```text
Batch ID
포함 Chunk 수
Raw Code Token
분석 과정
추가 Context Request
Pull된 Chunk
Candidate Finding
Verifier 결과
총 Token 사용량
Baseline 대비 Token 변화
```

예:

```text
Batch 01
- Initial Chunks: 18
- Initial Tokens: 9,240
- Context Requests: 2
- Additional Chunks Pulled: 3
- Findings: 2
- VERIFIED: 1
- REJECTED: 1
- Total Input Tokens: ...
- Baseline Tokens: ...
- Reduction: ...%
```

취약점이 하나도 나오지 않는 Batch가 있어도 된다.
오히려 무조건 취약점을 생성하지 않는다는 점을 보여줄 수 있다.

---

# 12. 신뢰도 설계

AI SAST의 가장 큰 위험 중 하나는 Hallucination과 False Positive이다.

따라서 다음을 반드시 지킨다.

## Evidence Required
Finding은 반드시 실제 Chunk와 line 정보에 연결해야 한다.

## No Evidence, No Finding
증거가 부족하면 Finding을 확정하지 않는다.

## Context Request
문맥이 부족한 경우:
- 추측 금지
- `NEED_CONTEXT`

## Independent Verification
Verifier는 Analyst의 결론을 그대로 이어받지 않고 Evidence를 다시 확인한다.

## Final Status
```text
VERIFIED
REJECTED
INCONCLUSIVE
```

---

# 13. Non-Goals

v1에서 다음은 과도하게 구현하지 않는다.

- 완전한 Symbolic Execution Engine
- 완전한 Interprocedural Taint Engine
- SAT/SMT Solver 기반 Formal Verification
- 복잡한 Race Condition / Concurrency 분석
- 모든 C/C++ Build Configuration 완전 분석
- 전체 CWE 자동 보장
- Dynamic Analysis / Fuzzing

이 과제의 핵심은:
- 코드 분할
- 대규모 Repository 탐색
- AI 보안 분석
- Multi-Agent
- Context 효율
- Evidence 검증
을 실제로 동작시키는 것이다.

---

# 14. 구현 권장 스택

구현은 최대한 단순하고 빠르게 한다.

## Language
- Python

## Parsing
- Tree-sitter 또는 Tree-sitter C/C++
- 필요 시 Clang 계열 도구 검토 가능하나 v1에서는 복잡도를 최소화할 것

## LLM
- 설정 파일 또는 환경변수로 Model/API 교체 가능하게 구성

## Storage
초기 버전은 JSON/JSONL 파일이면 충분하다.

예:

```text
data/
  chunks/
  index/
  batches/
  findings/
  logs/
```

Vector DB는 필수 아님.

---

# 15. 권장 Repository 구조

```text
ai-sast/
├── README.md
├── SUBMISSION.md
├── AI_SAST_Codex_Spec_v3.md
├── requirements.txt
├── .env.example
├── config.yaml
├── target.lock.yaml
├── experiment.lock.yaml
│
├── schemas/
│   └── target-lock.schema.json
│
├── artifacts/
│   ├── coverage/
│   │   ├── target_inventory.json
│   │   ├── source_manifest.jsonl
│   │   ├── exclusion_manifest.jsonl
│   │   └── target_verification.json
│   └── evaluation/
│       └── selection.json
│
├── src/
│   ├── chunking/
│   │   ├── parser.py
│   │   ├── chunker.py
│   │   └── tokenizer.py
│   │
│   ├── index/
│   │   ├── code_index.py
│   │   ├── symbol_index.py
│   │   └── relation_index.py
│   │
│   ├── agents/
│   │   ├── analyst.py
│   │   ├── context_agent.py
│   │   └── verifier.py
│   │
│   ├── tools/
│   │   ├── get_chunk.py
│   │   ├── get_symbol.py
│   │   ├── get_callers.py
│   │   └── get_callees.py
│   │
│   ├── orchestration/
│   │   └── pipeline.py
│   │
│   └── telemetry/
│       └── token_logger.py
│
├── prompts/
│   ├── development/
│   │   └── README.md
│   ├── runtime/
│   │   ├── analyst_system.md
│   │   ├── context_system.md
│   │   └── verifier_system.md
│   └── prompt_evolution.md
│
├── results/
│   ├── batch_01/
│   ├── batch_02/
│   ├── batch_03/
│   └── external_sast/
│       └── <tool>/
│           ├── run_metadata.json
│           ├── raw.*
│           ├── normalized.json
│           └── comparison.md
│
├── docs/
│   ├── target_profile.md
│   ├── architecture.md
│   └── agent_skills.md
│
├── report/
│   └── report.md
│
├── presentation/
│   ├── slides.pdf
│   ├── demo_script.md
│   └── rehearsal_checklist.md
│
├── scripts/
│   ├── lock_target.ps1
│   ├── verify_target_lock.ps1
│   ├── verify_target_lock.py
│   └── reproduce.py
│
└── tests/
```

---

# 16. Codex 개발 순서

한 번에 전체 시스템을 구현하지 않는다. 아래 Phase 0~16이 유일한 Canonical
순서이며, 각 단계의 완료 기준과 Artifact 검증을 통과한 뒤 다음 단계로 이동한다.

## Phase 0. Target Scope Contract

1. 공식 Userland URL, full Commit SHA, submodule 상태를 고정한다.
2. 고정 Git tree의 include 확장자와 path exclusion을 이유와 함께 고정한다.
3. Build/preprocessor 가정과 입력·신뢰 경계 profile을 작성한다.
4. 모든 tracked file을 in-scope/out-of-scope로 분류하고 file-list hash와 scope
   hash를 계산한다.
5. Target 계약이 바뀌면 새 Experiment ID와 모든 downstream artifact를 다시
   생성한다는 규칙을 고정한다.

완료 기준:

- `target.lock.yaml`, `schemas/target-lock.schema.json`,
  `docs/target_profile.md`에 placeholder가 없음
- 고정 Commit `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`에서 tracked
  830개가 in-scope 654개와 out-of-scope 176개로 정확히 분할됨
- tracked `.c` 284개, `.h` 367개, `.cpp` 3개에 path exclusion이 없음
- Manifest와 두 SHA-256을 독립적인 verify 실행에서 재계산해 일치함

## Phase 1. OllamaProvider Preflight and Experiment Lock

1. v1 Runtime Backend를 Ollama로 고정한다.
2. Endpoint, model name, immutable model digest, `num_ctx`, temperature, seed,
   tokenizer/counting 방식, retry와 timeout을 실제 환경에서 확인한다.
3. Baseline과 Proposed가 동일 모델·설정·고정 3-Batch를 사용하도록
   `experiment.lock.yaml`에 봉인한다.
4. System/User instruction, tool schema, structured state, chat-template overhead,
   reserved output, safety margin을 포함한 worst-case context envelope를 검증한다.

완료 기준:

- Health, model presence, digest, structured JSON smoke call이 모두 성공함
- `experiment.lock.yaml`에 추정값이나 placeholder가 없음
- Lock의 semantic 변경 시 새 Experiment ID가 발급됨

## Phase 2. Repository Parser, Structural Chunker, Batch Generator

1. Phase 0의 `source_manifest.jsonl`만 입력으로 사용하고, 각 원문은 local
   worktree가 아니라 manifest의 `git_blob_oid`로 고정 Git blob에서 읽는다.
2. 함수/타입/매크로/AST block을 구조적으로 Chunking하고 parse 실패는
   line-window fallback으로 반드시 매핑한다.
3. 모든 Chunk에 안정적 ID, file/line, content hash, token count를 부여한다.
4. Phase 1의 context envelope에 맞춰 결정적으로 Batch를 구성한다.

완료 기준:

- `parse_success + fallback_success + parse_error = 654`
- `unmapped_in_scope_ranges == 0`; 불가능한 파일은 실행 전 명시적 Scope 변경 필요
- 동일 Lock 입력에서 Chunk/Batch Manifest와 hash가 byte-identical하게 재생성됨

## Phase 3. Compact Code Index

Symbol, signature, file/line, caller/callee, type/macro definition, basic AST fact를
Chunk ID에 연결해 저장한다. 원문 전체나 검증되지 않은 취약점 결론은 Index에
저장하지 않는다.

완료 기준: 고정 fixture와 Userland symbol에서 결정적 검색 결과가 재현됨.

## Phase 4. Retrieval Tools

v1 필수 Tool을 하나의 canonical interface로 구현한다.

- `get_chunk(chunk_id)`
- `search_symbol(symbol)`
- `get_callers(symbol)`
- `get_callees(symbol)`
- `get_definition(symbol)`
- `get_file_chunks(file)`

완료 기준: Agent가 Repository를 임의 탐색하지 않고 Reference를 통해 필요한 최소
원문만 조회할 수 있으며 모든 반환에 Commit/File/Chunk/Line/Content Hash가 있음.

## Phase 5. Analyst Agent

할당된 Batch 전체를 취약점 유형 allow-list 없이 분석한다. 정상 결과는
`run_status=OK`, `batch_state=COMPLETE|NEED_CONTEXT`, `findings`,
`context_requests`, `inconclusive_items`로 표현한다. Candidate와 Context Request는
공통 `analysis_item_id`로 연결한다. 인프라/Schema 실패는 `run_status=ERROR`와
구조화된 `error`로 분리한다.

완료 기준: Schema valid fixture, NONE, Candidate, NEED_CONTEXT, ERROR branch가
모두 테스트됨.

## Phase 6. Context Agent

Index/Tool-first로 요청 목적에 필요한 최소 Evidence만 반환하고 취약 여부를
판정하지 않는다. 출력 field는 `selected_chunks`로 통일하고 찾지 못한 경우를
명시한다.

완료 기준: relevance/budget/중복 요청/`not_found` fixture가 통과함.

## Phase 7. Verifier Agent

Analyst의 장문 reasoning을 전달받지 않고 Claim과 Evidence Reference만 받아 원문을
독립 재조회한다. 반증 우선으로 safe path, guard, caller를 확인하고
`VERIFIED|REJECTED|INCONCLUSIVE`만 판정한다. Schema/호출 실패는 verdict가 아니라
`run_status=ERROR`다.

완료 기준: Evidence 없는 VERIFIED가 불가능하고 세 verdict와 ERROR branch가 모두
테스트됨.

## Phase 8. Orchestrator

`Batch -> Analyst -> Context 요청 반복 -> Analyst -> Verifier -> Final Finding`을
결정적으로 연결한다. 반복 Context Request는 정상 종료
`INCONCLUSIVE reason=REPEATED_CONTEXT_REQUEST`로 처리한다. Retry 소진,
Tool/Schema/Provider 오류는 구조화된 ERROR Artifact로 남긴다.

완료 기준: 세 Agent가 실제로 호출된 integration trace와 모든 종료 경로가 있음.

## Phase 9. Telemetry

모든 성공/실패/재시도 LLM 호출에 model digest, Agent, Batch, purpose,
`input_tokens`, `output_tokens`, system/user/evidence/tool-schema/structured-state
component token, retry index, cache hit, elapsed time, run status를 기록한다.

완료 기준: 합계가 component 및 provider counter와 대조되고 raw JSONL에서 report
수치로 역추적 가능함.

## Phase 10. Three-Batch Proposed Execution

결과를 보기 전에 `artifacts/evaluation/selection.json`으로 고정한 3개 Batch를
실제 Userland에서 실행한다. 실패 시 Batch를 교체하지 않고 같은 ID를 수정 후
재실행한다.

완료 기준: 세 Batch 모두 schema-valid 결과, 실제 Agent trace, raw telemetry를
가지며 하나라도 유효 결과가 없으면 Phase 미완료.

## Phase 11. Paired Internal Token Baseline

동일 Lock과 3-Batch에서 모든 Context/Verifier LLM 호출에 원 Batch와 그 시점까지
Pull된 Raw Chunk 누적분을 반복 첨부하는 Baseline을 cold-cache로 실행한다.
Proposed와 모델/digest/temperature/seed/retry/counter 조건을 동일하게 통제한다.

완료 기준: 모든 실제·실패·재시도 호출의 input+output token을 합산하고, 한쪽이
ERROR/미완료이면 reduction을 비교값으로 보고하지 않음.

## Phase 12. Manual Adjudication

3-Batch의 Finding을 고정 rubric으로 source evidence에 따라 수동 검토한다. Ground
truth가 없는 경우 precision/recall을 주장하지 않고 VERIFIED/REJECTED/불충분과
검토 근거를 남긴다.

완료 기준: 모든 Candidate/최종 판정이 reviewer record와 raw evidence에 연결됨.

## Phase 13. External SAST Evaluation

최소 1개 실제 외부 도구(예: Cppcheck)를 동일 Target Commit과 Scope에서 실행한 뒤
primary location이 3-Batch 영역에 속한 결과를 매핑한다. 내부 Token Baseline과는
별도 비교다.

완료 기준: version, exact command, config/ruleset 또는 default, elapsed time,
raw/normalized output, overlap/AI-only/tool-only 비교와 한계를 저장함.

## Phase 14. Core Documentation and Reproducibility

README, Architecture Mermaid, Agent Skill, Development/Runtime Prompt, Report,
`scripts/reproduce.py`를 완성한다. Report 수치는 raw artifact 경로를 포함한다.

완료 기준: clean checkout 기준 reproduce smoke run과 모든 내부 링크가 통과함.

## Phase 15. Presentation and Demo

문제 -> 구조 -> 실제 3-Batch Evidence -> Token 실측 -> 외부 도구 비교 -> 한계
순서의 slides와 Demo Script를 만들고 recorded-result fallback을 포함해 rehearsal한다.

완료 기준: `slides.pdf`, `demo_script.md`, `rehearsal_checklist.md`가 있고 rehearsal
항목이 모두 통과함.

## Phase 16. GitHub Publication and Final Submission Packaging

Secret과 Target source vendoring 여부를 확인하고 remote에 push한다. `SUBMISSION.md`에
Target Commit SHA와 제출 Commit full SHA를 구분해 기록하고 불변 Commit URL로 모든
산출물 접근성을 검증한다. Tag는 선택적 별칭일 뿐 제출 기준은 full SHA다.

완료 기준: 제출 URL에서 지정 full SHA의 모든 산출물이 열리고 placeholder·깨진
링크·추적 불가능한 수치가 없음.

---

# 17. Prompt 작성 원칙

Prompt 자체도 평가 대상이므로 단순히 최종 Prompt만 남기지 않는다.

## Prompt Evolution 기록
`prompts/prompt_evolution.md`에 다음을 기록한다.

```text
Idea
-> Prompt v1
-> 문제
-> 개선
-> Prompt v2
-> 변경 이유
```

예:

```text
Idea:
없는 Symbol을 AI가 만들어내면 안 된다.

Prompt v1:
"Do not hallucinate."

문제:
행동 지침이 모호하다.

Prompt v2:
"Do not infer any symbol, caller, callee, validation, or code path
that is not present in the supplied evidence.
If required evidence is missing, return NEED_CONTEXT."

개선 이유:
추상적인 Hallucination 금지를 실제 Agent 행동 규칙으로 변환.
```

---

# 18. Analyst Prompt에 반드시 포함할 철학

최종 문구는 개발 과정에서 조정하되 의미는 유지한다.

```text
- Analyze the provided C/C++ code for statically inferable security weaknesses.
- Do not restrict the analysis to a fixed CWE list.
- Known vulnerability categories are a minimum checklist, not an exhaustive scope.
- Consider semantic and logical security flaws in addition to dangerous API patterns.
- Never invent missing code, symbols, callers, callees, validations, or runtime behavior.
- If additional repository context is required, return NEED_CONTEXT with a precise request.
- Every candidate finding must reference concrete source evidence.
- If the available static evidence is insufficient, return INCONCLUSIVE rather than guessing.
```

---

# 19. Verifier Prompt 핵심

```text
- Treat the Analyst's claim as an untrusted hypothesis.
- Re-read the referenced source evidence.
- Attempt to disprove the claim.
- Check for missing guards, caller-side validation, alternative control flow,
  cleanup logic, size constraints, and other evidence that may invalidate the finding.
- Do not assume code that is not present.
- Return exactly one final status:
  VERIFIED / REJECTED / INCONCLUSIVE.
```

---

# 20. 최종 Report에서 강조해야 할 점

## 문제
대규모 Repository를 LLM에 통째로 입력할 수 없다.

## 단순 해결책의 한계
단순 Chunking만 하면 함수 간 Context가 끊길 수 있다.
또한 Multi-Agent 간 동일한 Raw Code가 반복 전달되면 Token이 낭비된다.

## 우리의 해결
1. 모든 코드를 구조적으로 Chunking
2. 각 Batch를 AI가 포괄적으로 분석
3. 추가 문맥은 Compact Code Index로 탐색
4. 필요한 Raw Code만 Evidence Pull
5. Agent 간에는 Raw Code 대신 Reference 중심으로 전달
6. Candidate는 독립 Verifier가 재검증

## 핵심 가치
- Coverage를 단순 Rule Filter로 희생하지 않음
- 대형 Repository 탐색 가능
- 불필요한 Raw Context 반복 감소
- AI Hallucination 억제
- 분석 과정과 Evidence 추적 가능
- 실제 Token 사용량으로 효율성 증명 가능

---

# 21. 프로젝트 핵심 문장

발표/README/Report에서 다음 메시지를 일관되게 사용한다.

> **모든 Agent가 모든 코드를 읽는 SAST가 아니라, 필요한 Agent가 필요한 증거만 읽는 SAST.**

또는:

> **Code once, reference thereafter.**

그리고 기술적으로는:

> **대규모 C/C++ Repository를 구조적으로 분할하고, AI가 각 Batch를 포괄적으로 분석한 뒤 부족한 문맥만 Compact Code Index를 통해 On-Demand로 조회하며, Agent 간에는 Evidence Reference 중심으로 협업하는 Multi-Agent SAST.**

---

# 22. 구현 시 주의사항

1. API Key를 Repository에 Commit하지 않는다.
2. `.env`는 `.gitignore` 처리한다.
3. `.env.example`만 제공한다.
4. Target Repository 자체를 결과 Repository에 통째로 복사하지 않아도 된다.
5. 분석 대상 Commit/Revision을 기록해 재현성을 확보한다.
6. Finding을 실제 CVE처럼 과장하지 않는다.
7. AI 출력만으로 취약점을 확정하지 않고 Verifier 결과를 함께 기록한다.
8. Token 절감률은 실제 측정값으로만 작성한다.
9. “세계 최초”, “모든 취약점 탐지” 같은 검증 불가능한 표현을 사용하지 않는다.
10. 개발 속도를 위해 불필요한 기능을 먼저 추가하지 않는다.

---

# 23. 최종 성공 조건

v1은 아래가 되면 성공이다.

- [ ] Raspberry Pi Userland를 자동으로 Chunking할 수 있음
- [ ] Chunk가 여러 Batch로 구성됨
- [ ] Analyst Agent가 Batch 전체를 분석함
- [ ] Analyst가 추가 Context를 요청할 수 있음
- [ ] Context Agent가 필요한 Chunk를 찾아 제공함
- [ ] Verifier가 Candidate를 독립 재검증함
- [ ] 최종 상태가 VERIFIED / REJECTED / INCONCLUSIVE로 기록됨
- [ ] 모든 Agent Role이 실제 코드로 구현됨
- [ ] 최소 3개 Batch 실행 결과가 저장됨
- [ ] Token 사용량이 Agent/Batch별로 기록됨
- [ ] Baseline과 Proposed 구조의 Token 사용량을 비교할 수 있음
- [ ] Prompt 및 Prompt 개선 과정이 Repository에 저장됨
- [ ] GitHub README 및 최종 Report가 작성됨

---

# 24. 구현 우선순위

시간이 부족한 경우 아래 우선순위를 따른다.

```text
MUST
Chunker
-> Batch
-> Analyst
-> Context Retrieval
-> Verifier
-> Token Logging
-> 3 Batch Results

SHOULD
Compact Code Index 고도화
Baseline 비교
Prompt Evolution 정리

COULD
추가 취약점 전용 Tool
고급 Call Graph
더 정교한 Ranking
UI
Vector DB
```

**MUST가 끝나기 전에는 COULD 항목을 구현하지 않는다.**

---

# 25. AI 활용 전략: 어디에서 AI를 쓰고 어디에서는 쓰지 않는가

이 프로젝트에서 AI는 모든 작업을 담당하지 않는다.

핵심 설계 원칙은 다음과 같다.

> **구조적으로 확정할 수 있는 일은 일반 프로그램이 수행하고, 코드의 의미와 보안 맥락을 판단해야 하는 일만 AI Agent가 수행한다.**

이를 통해:
- 불필요한 LLM 호출 감소
- Token 절약
- Hallucination 감소
- 결과 재현성 향상
- 개발 복잡도 감소
를 노린다.

## 25.1 AI를 사용하지 않는 영역

다음 작업은 Python + Tree-sitter 등 Deterministic Tool이 수행한다.

```text
Repository 파일 탐색
C/C++ 파일 식별
        ↓
AST Parsing
        ↓
Function / Type / Macro 단위 추출
        ↓
Chunk 생성
        ↓
Chunk ID 부여
        ↓
Token Count 계산
        ↓
Symbol / Call / File Relation Index 생성
        ↓
Batch 구성
```

여기에서는 LLM을 호출하지 않는다.

### 이유

예를 들어 다음 정보는 AI의 의미 추론이 필요 없다.

```text
foo() 함수가 어느 파일 몇 번째 줄에 있는가?
foo()가 bar()를 호출하는가?
함수의 parameter는 무엇인가?
Chunk의 Token 수는 얼마인가?
```

Parser와 정적 도구로 더 빠르고 일관되게 얻을 수 있기 때문이다.

---

## 25.2 AI를 사용하는 영역

AI는 다음 세 가지 의미 판단에 집중한다.

```text
1. 이 코드에 어떤 보안 문제가 존재할 가능성이 있는가?

2. 현재 코드만으로 판단할 수 없다면
   어떤 추가 코드가 필요한가?

3. Analyst가 제시한 Finding이 실제 Source Evidence로
   성립하는가?
```

이를 각각:

```text
Analyst Agent
Context Agent
Verifier Agent
```

가 담당한다.

---

# 26. Runtime AI Architecture

AI SAST가 실제 실행될 때의 AI 사용 흐름은 다음과 같다.

```text
Raw Repository
     |
     | Deterministic
     v
Chunk + Index + Batch
     |
     | LLM
     v
+---------------------------+
| Analyst Agent             |
|                           |
| "이 Batch에서 정적으로    |
|  판단 가능한 보안 문제를  |
|  포괄적으로 분석하라."    |
+-------------+-------------+
              |
       +------+------+
       |             |
     NONE        NEED_CONTEXT
       |             |
       |             v
       |     +--------------------+
       |     | Context Agent      |
       |     |                    |
       |     | "이 판단에 필요한 |
       |     |  코드를 Index에서 |
       |     |  찾아라."          |
       |     +---------+----------+
       |               |
       |        Selected Chunk
       |               |
       |               v
       |         Analyst 재분석
       |               |
       +---------------+
              |
         Candidate
              |
              v
+---------------------------+
| Verifier Agent            |
|                           |
| "이 주장을 믿지 말고      |
|  실제 Evidence를 다시     |
|  확인하여 반증하라."      |
+-------------+-------------+
              |
              v
VERIFIED / REJECTED / INCONCLUSIVE
```

---

# 27. Agent별 AI 사용 방식

## 27.1 Analyst Agent

### AI를 사용하는 이유

Analyst는 단순 Pattern Matching이 아니라 코드의 의미를 판단해야 한다.

예를 들어:

```c
if (len > max)
    return;

memcpy(dst, src, len);
```

에서 단순히 `memcpy()`가 있다는 이유로 취약하다고 판단하면 안 된다.

반대로 위험 API가 전혀 없어도:

```c
if (user->team == resource->team)
    return ALLOW;
```

같은 코드에서 시스템 요구사항에 따라 Authorization Logic 문제가 존재할 수도 있다.

이런 의미 기반 판단을 LLM에 맡긴다.

### 입력

```text
- Batch Raw Code
- Chunk ID
- File / Line Metadata
- 최소한의 Repository Context
- 이전 Context Request 결과(있는 경우)
```

### 출력

반드시 구조화한다.

```json
{
  "status": "NONE | NEED_CONTEXT | CANDIDATE | INCONCLUSIVE",
  "findings": [],
  "context_requests": [],
  "reason": ""
}
```

### AI에게 허용되는 행동

```text
코드 의미 분석
취약점 가설 생성
추가 문맥 필요성 판단
Evidence 위치 지정
```

### AI에게 금지되는 행동

```text
없는 함수 생성
없는 Caller 가정
Runtime 동작 상상
Evidence 없이 취약점 확정
Target Source 자체 수정
```

---

## 27.2 Context Agent

### AI를 사용하는 이유

Context Agent는 단순 검색기가 아니다.

Analyst가:

```text
"process_data()가 실제 외부 입력을 받는 경로인지 확인하고 싶다."
```

라고 했을 때 단순히 모든 Caller를 반환하면 Context가 다시 커질 수 있다.

Context Agent는 Compact Code Index를 보고:

```text
현재 판단 목적에 가장 직접적으로 필요한 Chunk가 무엇인지
```

선택한다.

### Tool 사용

Context Agent는 직접 Repository 전체를 Prompt에 받지 않는다.

다음 Tool을 호출한다.

```text
search_symbol(symbol)
get_chunk(chunk_id)
get_callers(symbol)
get_callees(symbol)
get_definition(symbol)
get_related_types(symbol)
get_macro_definition(name)
```

### AI 역할

```text
요청 목적 이해
↓
Index 검색 Tool 선택
↓
후보 결과 비교
↓
가장 필요한 Context 선택
↓
Analyst에게 반환
```

### 중요 원칙

한 번에 관련 코드 전체를 가져오지 않는다.

```text
필요한 최소 Context
→ Analyst에게 전달
→ 부족하면 추가 요청
```

방식으로 진행한다.

---

## 27.3 Verifier Agent

### AI를 사용하는 이유

Analyst와 동일한 LLM을 사용하더라도 역할과 Prompt를 분리하여
Finding을 독립적으로 다시 검증한다.

Verifier의 목적은:

```text
"왜 이 Finding이 맞는가?"
```

가 아니라:

```text
"이 Finding이 틀렸을 가능성은 없는가?"
```

를 검토하는 것이다.

### 입력

가능한 한 다음만 전달한다.

```json
{
  "claim": "...",
  "cwe_candidate": "...",
  "evidence_refs": [
    {
      "chunk_id": "C0184",
      "lines": "142-149"
    }
  ]
}
```

Analyst의 장문의 전체 추론 과정은 기본적으로 넘기지 않는다.

### Verifier 행동

```text
Evidence Raw Code 재조회
Caller-side validation 확인
Bounds / State / Error Handling 확인
Claim과 Source의 불일치 확인
Alternative Safe Path 확인
```

최종 출력:

```json
{
  "status": "VERIFIED | REJECTED | INCONCLUSIVE",
  "reason": "...",
  "evidence_refs": []
}
```

---

# 28. AI Tool Calling 설계

Agent는 Repository 내용을 자유롭게 탐색하는 것이 아니라
제한된 Tool Interface를 통해 필요한 정보만 가져온다.

## 필수 Tool 후보

```python
get_chunk(chunk_id)
search_symbol(symbol)
get_callers(symbol)
get_callees(symbol)
get_definition(symbol)
get_file_chunks(file_path)
```

필요 시 추가:

```python
get_macro_definition(name)
get_type_definition(name)
get_include_context(file_path)
```

## Tool 반환 원칙

가능한 한 Raw Repository 전체가 아니라 필요한 정보만 반환한다.

예:

```json
{
  "symbol": "process_data",
  "callers": [
    {
      "chunk_id": "C0031",
      "symbol": "handle_packet"
    },
    {
      "chunk_id": "C0812",
      "symbol": "dispatch_message"
    }
  ]
}
```

이후 실제 Raw Code가 필요한 Chunk만 `get_chunk()`로 가져온다.

---

# 29. AI Context 관리 전략

AI에게 제공하는 Context는 단계별로 제한한다.

## Analyst 초기 호출

```text
System Prompt
+
현재 Batch Raw Code
+
Chunk Metadata
```

불필요한 Repository 전체 Index는 전달하지 않는다.

## Context Agent

```text
System Prompt
+
Analyst의 정확한 Context Request
+
Compact Code Index 검색 결과
```

## Analyst 재호출

기존 Batch 전체를 무조건 다시 넣는 방식은 피한다.

가능한 구현 방식:

```text
Candidate 관련 기존 Chunk
+
새로 Pull된 Chunk
+
이전 분석의 짧은 Structured State
```

즉 Agent 상태를 긴 자연어 History가 아니라 JSON 형태로 압축해 유지한다.

예:

```json
{
  "candidate_id": "F01",
  "hypothesis": "possible OOB write",
  "known_evidence": ["C0184:142-149"],
  "unknowns": ["caller-side len validation"],
  "context_round": 1
}
```

---

# 30. AI 반복 분석 종료 조건

AI가 계속 Context를 요구하면 비용이 끝없이 증가할 수 있으므로 종료 규칙을 둔다.

## 종료 조건

다음 중 하나면 현재 Candidate 분석을 종료한다.

```text
1. 충분한 Evidence 확보
2. Safe condition 발견
3. 정적 분석으로 더 이상 판단 불가
4. max_context_rounds 도달
5. 동일 Context를 반복 요청
```

예:

```yaml
analysis:
  max_context_rounds: 3
```

이 값은 전체 Repository Token Budget이 아니다.

무한 반복 방지를 위한 안전장치이며 설정 가능하게 구현한다.

---

# 31. AI Model 설정

특정 Model에 종속되지 않도록 Provider Interface를 분리한다.

예:

```yaml
llm:
  provider: openai
  model: configurable
  temperature: low
```

또는 환경변수:

```text
LLM_PROVIDER=
LLM_MODEL=
OPENAI_API_KEY=
```

## 권장 원칙

Security Finding은 창작 작업이 아니므로:
- 낮은 Temperature
- Structured JSON Output
- 동일 Prompt Template
- Evidence-required policy
를 우선한다.

모델 선택은 코드에 Hard-code하지 않는다.

---

# 32. AI Failure Handling

LLM 호출 자체도 실패할 수 있으므로 다음을 처리한다.

```text
JSON Parse Failure
API Timeout
Rate Limit
Empty Output
Invalid Tool Request
Unknown Chunk ID
Repeated Context Request
```

## 처리 원칙

- 무조건 자동 재시도하지 않는다.
- 동일 Prompt 재시도 횟수 제한
- JSON Schema Validation
- 잘못된 Chunk ID 요청 시 명시적 오류 반환
- 실패 로그 저장

예:

```yaml
llm:
  max_retries: 2
```

---

# 33. AI 사용 로그

과제의 Prompt 평가와 Token 효율성 증명을 위해
모든 AI 호출을 로그로 남긴다.

각 호출 로그:

```json
{
  "batch_id": "B01",
  "agent": "analyst",
  "purpose": "initial_analysis",
  "model": "...",
  "input_tokens": 5241,
  "output_tokens": 811,
  "context_round": 0,
  "requested_chunks": [],
  "timestamp": "..."
}
```

Context Agent:

```json
{
  "batch_id": "B01",
  "agent": "context",
  "purpose": "caller_lookup",
  "input_tokens": 721,
  "output_tokens": 174,
  "requested_chunks": ["C0031"]
}
```

Verifier:

```json
{
  "batch_id": "B01",
  "agent": "verifier",
  "finding_id": "F01",
  "input_tokens": 1832,
  "output_tokens": 322,
  "result": "VERIFIED"
}
```

이 로그를 이용해 최종적으로:

```text
Agent별 Token 사용량
Batch별 Token 사용량
추가 Context 횟수
Raw Chunk 재전달 횟수
Baseline 대비 Token 절감
```

을 계산한다.

---

# 34. AI가 분석할 취약점 범위

AI SAST라는 목적에 맞게 특정 CWE 몇 개만 지원하는 구조로 제한하지 않는다.

Analyst에게는 다음을 **최소 체크 영역**으로 제공한다.

```text
Memory Safety
Buffer / Bounds
Integer / Size
Pointer / NULL
Memory Lifetime
Input Validation
Format String
Command / Path
File / Resource
Error Handling
Hardcoded Secret
Crypto/API Misuse
State Validation
Authorization / Logic
Trust / Validation Assumption
```

중요:

> 이 목록은 Allow-list가 아니다.

즉 이 목록에 없는 취약점이라도 제공된 Source Code로 정적으로 설명할 수 있다면
Finding 후보로 만들 수 있다.

---

# 35. AI 활용과 기존 SAST 기술의 관계

이 프로젝트는 기존 SAST를 버리는 것이 아니라 역할을 분리한다.

```text
Parser / AST / Index
       |
       | 정확한 구조적 사실
       v
AI Agent
       |
       | 의미 및 보안 문맥 판단
       v
Evidence-based Finding
```

Deterministic Tool이 잘하는 것:

```text
함수 위치
호출 관계
AST
Symbol
Type
Chunk
Token Count
```

AI가 잘할 수 있는 것:

```text
코드의 의도와 의미
여러 함수 사이의 보안 맥락
논리적 Validation 부족
추가로 어떤 Context가 필요한지
Finding에 대한 반증 검토
```

따라서 프로젝트의 AI 활용 철학은:

> **AI에게 모든 일을 시키지 않고, AI가 필요한 판단에만 AI를 사용한다.**

---

# 36. 개발 과정에서 Codex를 사용하는 방법

이 프로젝트 자체의 Runtime Agent와 별도로,
**개발 도구로 Codex를 사용한다.**

둘을 혼동하지 않는다.

```text
Codex
= AI SAST 프로그램을 개발하는 Coding Agent

Analyst / Context / Verifier
= 완성된 AI SAST 안에서 실제 Source Code를 분석하는 Runtime Agents
```

## Codex 활용 원칙

Codex에게 한 번에:

```text
"전체 AI SAST를 만들어라"
```

라고 요청하지 않는다.

Phase별로 개발한다.

```text
Phase 1
Structural Chunker 구현

Phase 2
Compact Code Index 구현

Phase 3
Retrieval Tool 구현

Phase 4
LLM Provider Interface 구현

Phase 5
Analyst Agent 구현

Phase 6
Context Agent 구현

Phase 7
Verifier Agent 구현

Phase 8
Pipeline 연결

Phase 9
Telemetry 및 Token Logging

Phase 10
3 Batch 실험 / Baseline / Report
```

각 Phase마다:

```text
1. 요구사항 전달
2. Codex 구현
3. 테스트 실행
4. 결과 확인
5. 필요한 수정 Prompt 기록
6. 다음 Phase 진행
```

순서를 따른다.

---

# 37. Codex Prompt 기록

과제의 평가 기준 중 하나가 “아이디어를 AI에게 효과적으로 Prompt화한 방법”이므로
Codex와의 개발 Prompt도 반드시 보존한다.

권장 구조:

```text
prompts/
├── development/
│   ├── 00_architecture.md
│   ├── 01_chunker.md
│   ├── 02_indexer.md
│   ├── 03_retrieval_tools.md
│   ├── 04_analyst_agent.md
│   ├── 05_context_agent.md
│   ├── 06_verifier_agent.md
│   ├── 07_orchestrator.md
│   └── 08_telemetry.md
│
├── runtime/
│   ├── analyst_system.md
│   ├── context_system.md
│   └── verifier_system.md
│
└── prompt_evolution.md
```

## 반드시 구분할 것

```text
Development Prompt
= Codex에게 프로그램을 어떻게 구현해달라고 했는가

Runtime Prompt
= 완성된 프로그램이 실제 분석 시 LLM Agent에게 무엇을 지시하는가
```

최종 Report에는 둘 다 설명하는 것이 안전하다.

---

# 38. AI 활용 성공 기준

AI를 많이 호출하는 것이 목표가 아니다.

다음이 성공 기준이다.

```text
[ ] Analyst가 위험 API Pattern에 한정되지 않고 Batch를 포괄적으로 분석
[ ] 정보 부족 시 추측하지 않고 NEED_CONTEXT 반환
[ ] Context Agent가 전체 Repository 대신 관련 Chunk만 조회
[ ] Analyst가 추가 Context를 이용해 판단을 갱신
[ ] Verifier가 Evidence를 독립적으로 재검증
[ ] 각 AI 호출의 Token 사용량 기록
[ ] Agent 간 Raw Source 중복 전달 감소
[ ] AI 출력이 실제 Source Reference로 추적 가능
```

즉 핵심은:

> **AI 사용량을 최대화하는 것이 아니라, AI가 의미 판단에 가장 효율적으로 사용되도록 만드는 것.**

---

# 39. Runtime LLM Backend: Ollama

이 프로젝트의 Runtime Agent는 Prompt만으로 동작하지 않는다. 실제 추론을 수행할 LLM 실행 계층이 필요하며, v1에서는 **Ollama를 기본 Runtime Backend**로 사용한다.

```text
Codex
= 개발용 Coding Agent
        ↓
Python AI SAST
├─ Structural Chunker
├─ Compact Code Index
├─ Retrieval Tools
├─ Orchestrator
├─ Token Telemetry
└─ LLM Provider
      ↓
   Ollama Server
      ↓
   Local LLM
      ├─ Analyst Prompt
      ├─ Context Prompt
      └─ Verifier Prompt
```

구성요소의 역할은 다음과 같다.

- **Codex**: AI SAST 프로그램 자체를 개발한다.
- **Python**: 전체 파이프라인과 Tool 호출을 제어한다.
- **Ollama**: Local LLM을 실제로 실행한다.
- **LLM Model**: 코드 의미와 보안 맥락을 추론한다.
- **Runtime Prompt**: 동일 LLM에 Analyst / Context / Verifier 역할을 부여한다.
- **Orchestrator**: 어느 Agent를 언제 호출하고 어떤 결과를 다음 단계에 넘길지 제어한다.

Multi-Agent라고 해서 Agent마다 다른 모델을 사용할 필요는 없다. v1에서는 **동일 Ollama 모델 + 서로 다른 System Prompt + 서로 다른 입력 Context + 서로 다른 JSON Schema**로 역할을 분리한다.

# 40. Ollama 사용 이유

v1에서 Ollama를 기본 Backend로 사용하는 이유:

1. Local LLM 기반 구현이 가능하다.
2. Source Code를 외부 API로 전송하지 않는 구성이 가능하다.
3. Python에서 호출하기 쉽다.
4. 동일 모델을 여러 Agent Role에서 재사용할 수 있다.
5. Agent별 Prompt/Context를 직접 통제하기 쉽다.
6. 이후 모델 교체가 비교적 쉽다.

Ollama 자체를 프로젝트가 설치해주는 기능은 v1 범위에 포함하지 않는다.

# 41. 실제 Runtime 흐름

```text
1. Repository Parsing
   Python + Tree-sitter
   LLM 사용 안 함

2. Chunk / Batch 생성
   LLM 사용 안 함

3. Compact Code Index 생성
   LLM 사용 안 함

4. Batch → Analyst Agent
   Python → Ollama → Local LLM

5. Analyst 결과
   NONE / NEED_CONTEXT / CANDIDATE / INCONCLUSIVE

6. NEED_CONTEXT
   → Context Agent
   → Index/Retrieval Tool 사용
   → 필요한 Chunk만 선택
   → Raw Code Pull

7. Analyst 재분석

8. Candidate
   → Verifier Agent
   → Evidence Ref 기반 Raw Code 재조회

9. 최종 결과
   VERIFIED / REJECTED / INCONCLUSIVE
```

핵심은 **구조적으로 확정 가능한 작업은 Tool이 하고, 의미 판단이 필요한 부분만 Ollama의 LLM에게 맡기는 것**이다.

# 42. LLM Provider 추상화

Agent 코드에서 Ollama 구현에 직접 의존하지 않도록 Provider Interface를 둔다.

```text
LLMProvider
 ├─ OllamaProvider   ← v1 필수
 └─ OtherProvider    ← future/optional
```

예시:

```python
class LLMProvider:
    def chat(self, system_prompt, user_prompt, response_schema=None):
        raise NotImplementedError
```

```python
class OllamaProvider(LLMProvider):
    def __init__(self, base_url, model):
        self.base_url = base_url
        self.model = model

    def chat(self, system_prompt, user_prompt, response_schema=None):
        ...
```

각 Agent는 다음처럼 Provider만 호출한다.

```python
result = llm.chat(
    system_prompt=analyst_prompt,
    user_prompt=batch_context,
    response_schema=ANALYST_SCHEMA,
)
```

# 43. Ollama 설정

예시:

```yaml
llm:
  provider: ollama
  base_url: http://localhost:11434
  model: configurable-model-name
  temperature: 0.1
  max_retries: 2

analysis:
  max_context_rounds: 3

batch:
  max_batch_tokens: configurable
```

환경변수도 지원 가능하다.

```text
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=<model>
```

원칙:

- 모델명은 코드에 Hard-code하지 않는다.
- Batch Token 한도는 실제 모델 Context Window를 고려해 설정한다.
- `max_context_rounds`는 전체 Token Budget이 아니라 무한 반복 방지용 운영 한도다.

# 44. Ollama Preflight Check

분석 시작 전에 최소한 다음을 확인한다.

```text
[1] Ollama Server 연결 가능 여부
[2] 설정된 Model 사용 가능 여부
[3] Test Prompt 응답 여부
[4] Structured JSON 응답 처리 여부
```

실패 시 분석을 시작하지 말고 명확한 오류를 출력한다.

# 45. Runtime Prompt는 실제 실행 파일이다

다음 Prompt는 제출용 문서가 아니라 실제 Ollama 호출에 사용한다.

```text
prompts/
└─ runtime/
   ├─ analyst_system.md
   ├─ context_system.md
   └─ verifier_system.md
```

예:

```python
analyst_prompt = load_prompt("prompts/runtime/analyst_system.md")

result = llm.chat(
    system_prompt=analyst_prompt,
    user_prompt=batch_context,
)
```

따라서 Prompt 수정은 실제 SAST 동작에 직접 영향을 준다.

# 46. Analyst Agent + Ollama

입력:

```text
System Prompt
+
현재 Batch Raw Code
+
Chunk Metadata
+
Structured State(optional)
+
추가 Context(optional)
```

출력은 JSON으로 제한한다.

```json
{
  "status": "NEED_CONTEXT",
  "findings": [],
  "context_requests": [
    {
      "type": "callers",
      "symbol": "process_data",
      "reason": "caller-side validation 확인 필요"
    }
  ]
}
```

또는:

```json
{
  "status": "CANDIDATE",
  "findings": [
    {
      "title": "Potential out-of-bounds write",
      "category": "memory-safety",
      "evidence_refs": [
        {
          "chunk_id": "C0184",
          "start_line": 142,
          "end_line": 149
        }
      ]
    }
  ]
}
```

# 47. Context Agent + Ollama

Context Agent는 Raw Repository 전체를 Ollama에 넣지 않는다.

Analyst의 요청과 Compact Code Index/Retrieval 후보만 입력한다.

예:

```json
{
  "request": {
    "type": "callers",
    "symbol": "process_data",
    "reason": "caller-side validation 확인"
  },
  "candidates": [
    {"chunk_id": "C0031", "symbol": "handle_packet"},
    {"chunk_id": "C0812", "symbol": "dispatch_message"}
  ]
}
```

Context Agent가 필요한 Chunk만 선택한다.

```json
{
  "selected_chunks": ["C0031"],
  "reason": "direct caller and most relevant validation path"
}
```

단, **단순 Symbol 조회처럼 의미 판단이 필요 없는 경우에는 Context Agent LLM 호출을 생략하고 Tool 결과를 바로 사용할 수 있다.**

```text
정확한 단순 조회
→ Tool only

여러 후보 중 의미 판단 필요
→ Context Agent + Ollama
```

이 최적화를 허용한다.

# 48. Verifier Agent + Ollama

Verifier에는 Analyst의 긴 대화 History를 그대로 넘기지 않는다.

입력:

```text
Verifier System Prompt
+
Claim
+
Evidence Reference
+
해당 Evidence Raw Code
+
필요한 최소 Related Context
```

Verifier의 질문은:

```text
"왜 이 Finding이 맞는가?"
```

가 아니라:

```text
"이 Finding을 반증할 수 있는가?"
```

이다.

출력:

```json
{
  "status": "VERIFIED | REJECTED | INCONCLUSIVE",
  "reason": "...",
  "evidence_refs": []
}
```

# 49. Structured Output

모든 Agent 출력은 가능한 한 JSON으로 제한한다.

이유:

- Agent 간 전달 Token 감소
- Parsing 단순화
- Hallucination 추적 용이
- Telemetry 저장 용이
- Verifier 입력 최소화

권장 구조:

```text
schemas/
├─ analyst.schema.json
├─ context.schema.json
└─ verifier.schema.json
```

Schema 검증 실패 시 제한된 횟수만 재시도하며, 계속 실패하면 해당 분석을 `INCONCLUSIVE` 또는 오류 상태로 기록한다.

# 50. Ollama/LLM Usage 측정

각 LLM 호출에서 최소 다음을 기록한다.

```text
batch_id
agent
model
purpose
system_prompt_tokens
raw_code_tokens
metadata_tokens
additional_context_tokens
output_tokens
context_round
```

Provider가 실제 Usage 정보를 제공하면 같이 저장하되, Baseline과 Proposed 비교에서는 **동일한 Token Counter 기준**을 사용하는 것을 우선한다.

# 51. Local LLM을 전제로 한 설계

Local Model의 분석력이 충분하지 않을 수 있으므로 모델 하나에 모든 것을 맡기지 않는다.

보완 전략:

1. Batch를 너무 크게 만들지 않는다.
2. Repository 탐색은 Tool이 수행한다.
3. `NEED_CONTEXT`를 허용한다.
4. Evidence Reference를 강제한다.
5. Verifier를 별도 역할로 실행한다.
6. JSON Output을 사용한다.
7. 필요한 코드만 단계적으로 추가한다.

즉:

> **큰 모델에게 전체 Repository를 한 번에 던지는 대신, Tool과 작은 Context로 문제를 분해해 Local LLM이 판단하기 쉬운 형태로 만든다.**

# 52. Codex와 Runtime AI를 혼동하지 않는다

```text
[개발 단계]

사용자
  ↓
Codex
  ↓
AI SAST Python 프로그램 구현


[실행 단계]

Raspberry Pi Userland
  ↓
AI SAST
  ↓
Ollama
  ↓
Local LLM
  ↓
Analyst / Context / Verifier
```

따라서 Prompt도 두 종류다.

```text
Development Prompt
= Codex에게 프로그램을 어떻게 구현시켰는가

Runtime Prompt
= 완성된 SAST의 LLM Agent에게 무엇을 지시하는가
```

둘 다 보존한다.

# 53. Ollama 반영 후 v1 MUST 구현 목록

```text
[Deterministic]
[ ] C/C++ Repository Parser
[ ] Structural Chunker
[ ] Batch Generator
[ ] Compact Code Index
[ ] Retrieval Tools
[ ] Token Counter / Telemetry

[Runtime AI]
[ ] OllamaProvider
[ ] Ollama Preflight Check
[ ] Analyst Runtime Prompt
[ ] Context Runtime Prompt
[ ] Verifier Runtime Prompt
[ ] Structured JSON Output
[ ] Context Request Loop
[ ] Evidence Pull
[ ] Independent Verification

[Evaluation]
[ ] 최소 3개 Batch 실제 실행
[ ] Agent/Batch별 Token 로그
[ ] Evidence 저장
[ ] VERIFIED / REJECTED / INCONCLUSIVE
[ ] Baseline 비교

[Development Evidence]
[ ] Codex Development Prompt 저장
[ ] Runtime Prompt 저장
[ ] Prompt Evolution 기록
```

# 54. Codex에 전달할 구현 시작 지시문

```text
AI_SAST_Codex_Spec_v3.md를 프로젝트 최상위 명세로 읽어라.

v1의 Runtime LLM Backend는 Ollama다.
Prompt 파일만 작성하고 Agent 구현을 끝냈다고 간주하지 마라.

Python 프로그램이 실제 Ollama Server를 호출하여
Analyst / Context / Verifier Agent를 실행해야 한다.

구조적으로 확정 가능한 작업은 LLM에 맡기지 말고
Python + Tree-sitter 기반 Deterministic Tool로 구현하라.

동일 Ollama Model을 사용해도 되지만,
각 Agent는 서로 다른 Runtime System Prompt,
입력 Context, Tool 접근 방식, JSON Output Schema를 가져야 한다.

한 번에 전체 시스템을 구현하지 말고 다음 순서로 진행하라.

1. Structural Chunker
2. Compact Code Index
3. Retrieval Tools
4. OllamaProvider
5. Analyst Agent
6. Context Agent
7. Verifier Agent
8. Orchestrator
9. Token Telemetry
10. 3 Batch Evaluation

각 Phase가 실제로 실행되는 것을 테스트한 뒤 다음 단계로 진행하라.
```
