# Development Prompt Manifest

구현 과정에서 Codex에 전달된 사용자 Prompt와 그 해석, 변경, 검증 결과를 Canonical
Phase별로 보존한다. Runtime Agent Prompt와 혼동하지 않는다.

| Canonical Phase | 기록 |
|---|---|
| Phase 0 — Target Scope Contract | [phase_00_target_scope.md](phase_00_target_scope.md) |
| Phase 1 — OllamaProvider Preflight and Experiment Lock | [phase_01_ollama_preflight.md](phase_01_ollama_preflight.md) |

후속 Phase는 실제로 시작할 때 파일을 추가한다. 존재하지 않는 Prompt나 완료되지
않은 작업을 미리 작성하지 않는다.
