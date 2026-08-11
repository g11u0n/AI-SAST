# AI SAST

Raspberry Pi Userland를 구조적으로 분할하고, 역할이 분리된 Analyst,
Context, Verifier Agent로 정적 보안 분석을 수행하는 과제 프로젝트다.

## 현재 진행 상태

- Phase 0 — Target Scope Contract: 완료
- Phase 1 이후: 아직 시작하지 않음

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

## 추적 정책

Target checkout, 가상환경, cache, 임시 파일은 Git에서 제외한다. 반면 분석
evidence, Agent trace, telemetry, prompt, 평가 결과, report는 최종 제출 근거이므로
무시하지 않는다.
