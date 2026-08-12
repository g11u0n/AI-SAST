# AI SAST

Raspberry Pi Userland 대상 Multi-Agent 정적 보안 분석 프로젝트입니다.

## 최종 결과

-   Target files: 654
-   Chunks: 26,645
-   Batches: 2,055
-   최종 분석 Batch: 3
-   Analyst / Context / Verifier 호출: 3 / 21 / 21
-   Finding 후보: 21
-   VERIFIED: 0
-   REJECTED: 21
-   Agent 간 Evidence handoff 감소: 69.34%

상세 설계와 실험 결과는
[`AI SAST_Report_서민주.md`](AI%20SAST_Report_%EC%84%9C%EB%AF%BC%EC%A3%BC.md)를
참고하십시오.

## 구조

``` text
artifacts/   구축 및 재현성 Artifact
docs/        Target 설명
prompts/     Runtime Agent Prompt
schemas/     Structured Output / Artifact Schema
scripts/     Build, verify, analysis entrypoint
src/         Chunking, Index, Provider, Agent Runtime
tests/       Contract verification
```

Target checkout, 가상환경, cache, 임시 파일은 제출 Repository에 포함하지
않습니다.
