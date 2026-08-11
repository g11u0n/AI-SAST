# AI SAST

Raspberry Pi Userland를 구조적으로 분할하고, 역할이 분리된 Analyst,
Context, Verifier Agent로 정적 보안 분석을 수행하는 과제 프로젝트다.

## 현재 진행 상태

- Phase 0 — Target Scope Contract: 완료
- Phase 1 — OllamaProvider Preflight and Experiment Lock: 완료
- Phase 2 이후: 아직 시작하지 않음

단계는 [AI_SAST_Codex_Spec_v3.md](AI_SAST_Codex_Spec_v3.md)의
Canonical 개발 순서를 따른다. 한 단계의 완료 기준을 검증한 뒤 다음 단계로
넘어간다.

## 고정 분석 대상

- Repository: <https://github.com/raspberrypi/userland.git>
- Commit: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
- Scope: 해당 Git tree에 추적된 모든 `.c`, `.h`, `.cpp` 파일
- Path exclusion: 없음

상세 범위와 가정은 [target.lock.yaml](target.lock.yaml) 및
[docs/target_profile.md](docs/target_profile.md)에 있다.

## Phase 0 재현

PowerShell, Git, Python 3.11 이상이 필요하다. Target source는 제출 Repository에
포함하지 않는다.

```powershell
git clone --no-checkout https://github.com/raspberrypi/userland.git .target-src/userland
git -C .target-src/userland checkout --detach a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/lock_target.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_target_lock.ps1
python scripts/verify_target_lock.py --repo .target-src/userland
powershell -NoProfile -ExecutionPolicy Bypass -File tests/phase0_contract.ps1
```

Windows에서 Git이 `PATH`에 없으면 clone/checkout에는
`C:\Program Files\Git\cmd\git.exe`를 직접 사용한다. Phase 0 PowerShell script는
이 표준 설치 경로를 자동 탐지하며 필요하면 `-GitExecutable`로 명시할 수 있다.

`target.lock.yaml`은 YAML 1.2에서 유효한 strict JSON 표현을 사용한다. 따라서
Phase 0 검증에는 별도 YAML 패키지가 필요하지 않으며, 이후 Python 구현에서는
일반 YAML loader로 같은 파일을 읽을 수 있다.

## Phase 1 재현

Ollama `0.32.8`과 아래 immutable digest의 모델이 필요하다. 모델 binary는 크기가
4.7 GB이므로 제출 Repository에 포함하지 않는다.

```text
qwen2.5-coder:7b-instruct-q4_K_M
dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364
```

```powershell
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
python scripts/verify_experiment_lock.py
python scripts/verify_experiment_lock.py --live
python tests/phase1_contract.py
```

저장된 6개 high-water prefix 요청까지 실제로 재실행하려면 시간이 오래 걸리는
`python scripts/verify_experiment_lock.py --live-full`을 사용한다.

위 명령은 저장된 Lock과 evidence를 변경하지 않는다. Preflight evidence를 실제로
재생성할 때만 다음 명령을 별도로 실행한다. `--expected-digest`가 현재 tag drift를
쓰기 전에 차단한다.

```powershell
python scripts/ollama_preflight.py --model qwen2.5-coder:7b-instruct-q4_K_M --expected-digest dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364
```

`experiment.lock.yaml`도 YAML 1.2에 유효한 strict JSON이다. Phase 1에서는 아직
존재하지 않는 Batch ID를 만들지 않는다. 대신 3개 선택 수, seed, 결과 독립 ranking,
공통 selection artifact와 교체 금지 규칙을 고정하며, Phase 2에서 실제 Batch
Manifest와 selection hash를 결합할 때 새 Experiment ID를 발급한다.

현재 Runtime은 `num_ctx=8192`로 고정돼 있다. 입력 hard cap 6,144 tokens에 대해
6단계 누적 prefix-differential probe는 5,392 tokens(87.76%)였고, 모든 component가
각자의 cap 안에 있었다. reserved output 1,280 및 safety margin 768을 별도로
확보했다. 원시 요청·응답·component delta와 counter는
`artifacts/preflight/`에서 확인할 수 있다.

## 추적 정책

Target checkout, 가상환경, cache, 임시 파일은 Git에서 제외한다. 반면 분석
evidence, Agent trace, telemetry, prompt, 평가 결과, report는 최종 제출 근거이므로
무시하지 않는다.
