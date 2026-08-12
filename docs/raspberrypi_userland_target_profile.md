# Raspberry Pi Userland Target Profile

## Target identity

| 항목 | 고정값 |
|---|---|
| Upstream | `https://github.com/raspberrypi/userland.git` |
| Ref at lock | `refs/heads/master` |
| Commit | `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976` |
| Commit date | 2024-12-23 |
| License | BSD-3-Clause |
| Upstream state | 2025-08-27에 archive된 read-only/deprecated repository |

이 snapshot은 현재 제품 개발용 의존성을 추천하기 위한 것이 아니라 과제의 고정
분석 corpus다. Upstream README에 따르면 이 저장소는 Raspberry Pi의 ARM 측에서
VideoCore firmware와 통신하는 라이브러리를 담으며 EGL, MMAL, GLESv2, VCOS,
OpenMAX IL, VCHIQ ARM, `bcm_host`, WFC, OpenVG 등의 구성요소를 포함한다.

공식 근거:

- [Upstream repository와 README](https://github.com/raspberrypi/userland)
- [고정 commit의 README](https://github.com/raspberrypi/userland/blob/a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976/README.md)
- [고정 commit의 CMake 설정](https://github.com/raspberrypi/userland/blob/a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976/CMakeLists.txt)

## Scope decision

분석 범위는 고정 commit의 Git tree에 추적된 모든 `.c`, `.h`, `.cpp` 파일이다.
빌드 가능 여부, 테스트·예제 여부, 특정 API 포함 여부, 선택한 빌드 변형에서의
도달 가능성으로 소스를 사전 제거하지 않는다. 따라서 `containers/test/**`,
`interface/mmal/test/**`, `host_applications/**/hello_pi/**`, `opensrc/**`도
포함된다. Tracked C/C++ 경로에 대한 예외는 0개다.

Phase 2 이후의 parser는 local worktree byte가 아니라 source manifest의
`git_blob_oid`를 통해 고정 Commit의 Git blob을 읽는다. 따라서 Windows의
`core.autocrlf`, sparse checkout, index flag가 분석 입력을 바꾸지 않는다.

| 확장자 | 파일 수 |
|---|---:|
| `.c` | 284 |
| `.h` | 367 |
| `.cpp` | 3 |
| 합계 | 654 |

전체 tracked file은 830개이며 나머지 176개도
`artifacts/coverage/exclusion_manifest.jsonl`에 누락 없이 기록한다.

### Integrity identifiers

| 항목 | SHA-256 |
|---|---|
| In-scope target file list | `fb00da1a82fcd1e02b436c4f58cbd8391094901da8450b9881063db3558e6057` |
| Semantic target scope | `2d7910c98cd57192f9c80436d18d39be247a8b8e6705b98f831ee747b1d1d50a` |

## Explicit out of scope

- `.s` 1개: ARM assembly이며 v1 C/C++ parser 대상이 아니다.
- `.qasm` 16개와 `.qinc` 3개: VideoCore QPU assembly 계열이다.
- `.in` 8개: build/config template이며 concrete C/C++ parser 입력이 아니다.
- CMake, 문서, man page, hex/raw/media/font 등 비 C/C++ artifact.
- Git이 추적하지 않는 local build output.

`edidparser`와 `vcdbg`는 라이선스 사유로 upstream snapshot에 source가 없다고
README에 명시되어 있다. 이는 이 도구가 임의로 제외한 항목이 아니므로 coverage
분모에 포함하지 않는다.

## Build and preprocessing assumptions

- Upstream build는 CMake와 ARM cross compiler를 전제로 한다.
- ARM32/ARM64 등 모든 조건부 분기의 원문을 scope에 유지한다.
- Phase 0은 특정 build variant나 compilation database를 선택하지 않는다.
- Macro expansion, include resolution, build flags는 Phase 2 parser 계약에서
  고정한다. 그 전까지 preprocessor-dependent 의미는 검증되지 않은 것으로 본다.
- 전체 build 성공은 source coverage의 선행 조건이 아니다.

## Input and trust-boundary profile

아래 항목은 취약점 결론이 아니라 후속 정적 분석에서 확인할 탐색 가설이다.

1. Client application에서 exported Userland API로 들어오는 pointer, length,
   handle, path 등의 값.
2. Container/media metadata와 파일 기반 입력을 처리하는 host application 및
   component 경계.
3. ARM host와 VideoCore firmware 사이의 VCHIQ/MMAL/OpenMAX message 경계.
4. Device node, ioctl, shared buffer 등 process/kernel 또는 process/device 경계.
5. Compile-time macro와 platform별 type/ABI 차이로 생기는 조건부 안전성.

각 finding은 실제 source evidence와 호출/guard 관계로 입증해야 하며, 이 가설만으로
확정하지 않는다.

## Known limitations

- Archive된 오래된 codebase라 현재 Raspberry Pi stack을 대표하지 않는다.
- 실제 firmware/device runtime이 없으므로 v1은 정적 evidence만 평가한다.
- Platform macro와 복잡한 전처리 때문에 일부 파일은 parser fallback이 필요할 수
  있다. Phase 2에서는 `parse_success + fallback_success + parse_error = 654`를
  강제하고 어떤 파일도 조용히 누락하지 않는다.
- ARM assembly와 QPU assembly는 v1 분석 범위가 아니다.

## Experiment invalidation rule

Target commit, include extension, path exclusion, scope 판정 규칙 또는 scope hash가
바뀌면 기존 Batch와 결과를 재사용하지 않는다. 새 `experiment_id`를 발급하고
manifest, chunk, batch, Agent 결과, telemetry, 비교 결과를 모두 다시 생성한다.
