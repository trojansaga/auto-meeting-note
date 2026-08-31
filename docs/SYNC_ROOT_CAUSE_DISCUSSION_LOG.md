# A/V 싱크 근본원인 분석 — 3자 토론 전체 로그

- 작성일: 2026-08-20
- 주제: 화면 녹화 모드의 오디오/영상 싱크 오차 근본 원인과 근본 해결책
- 방식: 서로 다른 관점의 서브에이전트 3개가 독립 분석 → 상호 반박 → 조정자 판정 → 수렴
- 참가자
  - **관점 A** — macOS 캡처 시계(clock domain) 이론
  - **관점 B** — 구현 감사(implementation audit)
  - **관점 C** — 관측가능성(observability)·설계 대안 평가
- 정리본: [SYNC_ROOT_CAUSE_ANALYSIS.md](SYNC_ROOT_CAUSE_ANALYSIS.md)

## 토론에 주어진 공통 전제

모든 참가자에게 아래를 공통 입력으로 제공했다. "편차를 재측정하지 말고 구현과 이론을 근거로 분석하라"는 제약을 걸었고, 실제 녹화 실행은 금지했다.

### 현재 구조

- 영상과 오디오를 **서로 다른 두 개의 SCStream 인스턴스**로 캡처한다.
  - 영상: SCStream + `SCRecordingOutput`, `capture_audio=False`. mp4를 프레임워크가 직접 기록하며 프레임 콜백이 없다.
  - 오디오: 별도 SCStream, `capturesAudio=True`, `SCStreamOutputTypeAudio`/`SCStreamOutputTypeMicrophone` 콜백에서 raw PCM을 WAV로 기록.
- 싱크는 두 캡처의 "시작 시각"을 각각 wall clock(`time.time()`)으로 찍어 `offset = screen_anchor − audio_anchor`를 구하고, 병합 때 ffmpeg `-ss offset`(양수) 또는 `-itsoffset`(음수)로 오디오를 이동시킨다.
- pause/resume 시 세그먼트가 나뉘고, 세그먼트별 offset을 다시 계산해 trim 후 concat 한다.
- `setExcludesCurrentProcessAudio_(False)`.
- v1.1.13에서 SCRecordingOutput 오디오 인코딩의 popping 문제 때문에 `capture_audio=False`로 바꿨다는 주석이 있다.

### 선행 조사에서 실측으로 확정된 사실

1. `stream_capture_started_at`(SCStream `startCapture` 완료 핸들러의 `time.time()`)은 mp4 첫 프레임보다 0.14~1.09초 **이르다**. 실행마다 편차가 크다.
2. `active_segment_started_at`(`SCRecordingOutput` recordingDidStart 콜백)은 mp4 pts 0과 −40~−90ms로 비교적 일관.
3. 오디오 첫 콜백 도착 시각(`time.time()`)은 WAV 첫 샘플의 실제 캡처 시각보다 0.08~1.5초 **늦다**(탭 backlog burst).
4. CMSampleBuffer PTS를 `mach_absolute_time` 눈금으로 wall clock 환산해 오디오 앵커로 쓰면 개선되지만, 어떤 실행에서는 실측 대비 316ms 이르게 나왔다.
5. 진단 프로브의 클릭 예약 재생(AVAudioPlayer `playAtTime:`)은 한 실행 내에서 0.3ms 재현성. 단, 스피커 출력 지연의 절대값은 미지.
6. 수정 후 final 편차 실측: +17ms, −32ms, −409ms.

---

# 1라운드 — 독립 분석

각 에이전트가 다른 참가자의 의견을 보지 않은 상태에서 코드를 정독하고 작성한 포지션 페이퍼. 원문 그대로 수록한다.


## [1R] 관점 A — 시계 도메인 이론

### 관점 A: 시계 도메인 분석

#### 1. 핵심 주장 (3문장 이내)

현재 구조의 근본 결함은 **하드웨어가 찍어준 타임스탬프(CMSampleBuffer PTS = host time clock)와 소프트웨어 콜백 도착 시각(`time.time()`)을 뺄셈해서 offset을 만든다**는 것이다 — 영상 앵커는 `recordingOutputDidStartRecording:` 콜백의 wall clock(`continuous_screen_recorder.py:430`), 오디오 앵커는 PTS를 wall clock으로 환산한 값(`system_audio.py:158-175`)이므로, 두 값의 차에는 물리적 시각차가 아니라 **디스패치 큐 지연이 그대로 섞여 들어간다**. 게다가 WAV 기록 경로가 PTS를 버리고 샘플을 무조건 append하므로(`system_audio.py:387-396`) 탭 gap/디바이스 클럭 drift를 원리적으로 관측할 수 없고, 병합 단계에서는 `amix`가 두 번째 입력의 PTS를 무시하기 때문에 음수 offset(`-itsoffset`)이 **조용히 no-op이 된다**. 근본 해결은 "두 앵커를 같은 시계에서 얻고, PTS를 끝까지 보존한다" — 즉 영상 t=0도 wall clock이 아니라 host clock PTS로 관측하고, offset을 wall clock 뺄셈이 아니라 **PTS − PTS**로 계산하는 것이다.

#### 2. 시계 도메인 지도

| 타임스탬프 | 소속 시계 | 성질 | 관측 가능성 |
|---|---|---|---|
| `time.time()` | CLOCK_REALTIME (gettimeofday) | NTP로 step/slew 가능, 단조 아님 | 항상 |
| `mach_absolute_time()` (`system_audio.py:153-155`) | mach absolute = `CMClockGetHostTimeClock` | 단조, 슬립 중 정지, Apple Silicon 24MHz timebase | 항상 |
| SCStream **영상** 프레임 PTS / `SCStreamFrameInfoDisplayTime` | host time clock (mach absolute) — 문서상 "mach absolute time when the event occurred" | 윈도서버 합성 시각 | **SCRecordingOutput 사용 시 관측 불가** (프레임 콜백 없음) |
| SCStream **오디오** 버퍼 PTS | host time clock으로 정규화된 값 (문서에 명문 규정은 없음; 커뮤니티/샘플코드가 `hostTimeClock` 기준으로 다룸) | 탭 I/O proc이 믹서 출력을 얻은 시각 | 관측 가능 (현 코드가 읽음) |
| 오디오 디바이스 HAL 클럭 | 수정발진기 (48kHz ± ppm) | host clock 대비 **drift** 존재 | `AudioDeviceGetCurrentTime`으로 관측 가능하나 **현재 미사용** |
| 마이크 디바이스 클럭 | 시스템 오디오 탭과 **별개 도메인** | sys 대비 독립 drift | 미관측 |
| `AVAudioPlayer.deviceCurrentTime` | **출력 디바이스 클럭** | 문서: 재생/일시정지 중인 플레이어가 하나도 없으면 **0으로 리셋**. wall clock이 아님 | 진단 프로브의 기준 (`sync_diagnostics.py:267`) |
| 스피커 출력 지연(HAL latency + safety offset + BT 전송) | 위 어디에도 안 담겨 있음 | 디바이스별 10ms~300ms | `kAudioDevicePropertyLatency`/`SafetyOffset`로 관측 가능, **미조회** |
| mp4 pts 0 | 파일 내부 타임라인 | SCRecordingOutput이 첫 프레임을 0으로 재기준화(정확한 규약 비공개) | ffprobe로 사후 관측만 |

핵심 비대칭: **오디오 쪽은 하드웨어 시계를, 영상 쪽은 소프트웨어 시계를 쓰고 있다.** `_screen_video_anchor`(`recorder.py:341-362`)가 후보로 삼는 두 값은 둘 다 `time.time()`이며(`continuous_screen_recorder.py:424`, `:430`), 실측 사실 1·2가 바로 이 사실의 직접적 귀결이다 — `startCapture` 완료 핸들러는 파이프라인 warm-up 전이라 0.14~1.09초 편차(스케줄링 + 첫 프레임 대기)를 가지고, `recordingDidStart`는 첫 프레임 인제스트 직후에 오므로 −40~−90ms로 좁다. 즉 사실 1과 2는 "어느 앵커가 좋은가"의 문제가 아니라 **wall clock 콜백을 앵커로 쓰는 한 남는 잔차**다.

#### 3. 근본 원인 가설 (순위별)

##### H1. 영상 앵커가 wall clock 콜백이라 host clock 정밀도로 내려갈 수 없다 (확신도: 높음)
**이론적 근거**: 영상 PTS는 host clock에 있고 오디오 PTS도 host clock에 있으므로, 둘의 차는 **원리적으로 시계 변환 없이 정확히** 구할 수 있다. 그런데 코드는 `offset = wall(callback) − wall_converted(PTS)`를 계산한다(`recorder.py:615`, `:365-368`). 우변 두 항의 오차원이 다르므로 오차가 상쇄되지 않는다.
**설명하는 사실**: 1, 2 전부. 그리고 수정 후 잔차 +17ms/−32ms가 정확히 `recordingDidStart` 잔차(−40~−90ms) 스케일이라는 점.
**반증**: 동일 SCStream에 `SCStreamOutputTypeScreen` 출력을 추가로 붙여 첫 프레임 PTS를 얻고, `recordingDidStart`의 `time.time()`과 비교. 편차가 매 실행 −40~−90ms 범위에서 랜덤하면 H1 확정.

##### H2. 진단 프로브의 절대 기준이 출력 지연만큼 편향돼 있어, PTS 앵커가 "틀린 것"이 아니라 "기준이 틀린 것"일 수 있다 (확신도: 높음 — 사실 4 설명의 1순위)
**이론적 근거**: 프로브는 `deviceCurrentTime + lead`를 wall clock으로 환산해 클릭 발생 시각으로 쓴다(`sync_diagnostics.py:266-274`). 그러나 (a) `deviceCurrentTime`은 출력 디바이스 클럭이고 문서상 유휴 시 0으로 리셋되므로 wall clock과 위상이 고정돼 있지 않고, (b) 그 시각은 **믹서에 렌더링될 시각**이며 스피커에서 소리가 나오는 시각이 아니다. 한편 시스템 오디오 탭은 DAC보다 **앞선** 지점(믹서 출력)에서 샘플을 가져가므로, 탭 타임라인은 물리적 청취 시각보다 출력 지연 L만큼 앞서 있다. 이 관계를 풀면 `anchor_probe = anchor_true + L`, 즉 **PTS 기반 앵커는 프로브 기준 대비 항상 L만큼 이르게 보인다**. L은 내장 스피커 10~40ms, HDMI/디스플레이 스피커 50~100ms, Bluetooth/AirPods 150~300ms.
**설명하는 사실**: 4의 316ms — Bluetooth 출력 경로의 전형적 L 값과 정확히 같은 자릿수. 5("스피커 출력 지연의 절대값은 미지")가 이 가설의 전제를 스스로 인정하고 있다.
**반증**: 그 실행의 출력 디바이스를 확인하고, `kAudioDevicePropertyLatency + SafetyOffset + kAudioStreamPropertyLatency`를 조회해 합계가 ~316ms에 근접하는지 본다. 내장 스피커 강제 고정으로 재실행했을 때 316ms가 20~40ms로 줄면 확정. (역방향 성분: 유휴 디바이스 스핀업 때문에 실제 클릭이 `click_started_at`보다 **늦게** 나오면 부호가 반대인 오차가 생김 — −409ms 같은 이상치의 후보.)

##### H3. WAV 경로가 PTS를 버려서 탭 gap과 디바이스 클럭 drift가 누적 오차로 굳는다 (확신도: 중간~높음, 장시간 녹화에서 지배적)
**이론적 근거**: `write_sample_buffer`는 PCM만 append하고 PTS를 전혀 쓰지 않으며(`system_audio.py:387-396`), 콜백은 `num_samples == 0`이나 `writer.is_open == False`이면 그냥 return한다(`system_audio.py:461-470`). 즉 **탭이 버퍼를 드롭하면 그만큼 이후 전체 오디오가 영구히 앞으로 당겨진다** — 앵커를 완벽하게 맞춰도 복구 불가. 또한 WAV는 "48000 샘플 = 1초"라고 선언하지만 실제 캡처 레이트는 오디오 디바이스 클럭이다. 100ppm 오차면 1시간에 **360ms** 누적. 마이크는 또 다른 디바이스 클럭이라 sys 대비 독립적으로 drift한다.
**설명하는 사실**: 실행 간 편차의 꼬리(−409ms), "긴 회의일수록 끝부분이 어긋난다"류 증상.
**반증**: 각 버퍼의 PTS와 누적 샘플 수를 함께 로깅해 `(pts_last − pts_first)` vs `(total_samples/48000)`을 비교. 차이가 시간에 비례해 커지면 drift, 계단식으로 튀면 gap 드롭.

##### H4. 병합 단계에서 음수 offset이 사실상 무시된다 — 시계 문제가 아니라 컨테이너/필터 문제 (확신도: 높음, 검증 완료)
**이론적 근거**: FFmpeg `af_amix.c`는 출력 PTS를 **첫 번째 입력의 frame_list에서만** 가져오고 나머지 입력은 FIFO에 그냥 써 넣는다(입력별 PTS 정렬/헤드 무음 삽입 없음). `merge_audio_into_mp4`의 `[1:a:0][2:a:0]amix`(`recorder.py:1618`)에서 **mic에 붙인 `-itsoffset`은 완전히 no-op**이다. sys에 붙인 `-itsoffset`은 출력 PTS를 밀어 mov `elst` 빈 에디트로 기록되지만, 이는 데이터 trim과 다른 메커니즘이고 플레이어별로 해석이 갈린다. 추가로 `_audio_input_args`에는 **±50ms 데드존**이 있어(`recorder.py:310-313`) 50ms 이하 offset은 버려지고, AAC 인코더 프라이밍(1024~2112 샘플 = 21~44ms)이 그 위에 얹힌다. `_trim_wav`도 `offset <= 0.05`면 그냥 복사하므로(`recorder.py:1009-1012`) 세그먼트 경로에서 **음수 offset은 무음 프리펜드 없이 버려진다**.
**설명하는 사실**: +17ms/−32ms(데드존+프라이밍 크기와 일치), 그리고 pause/resume가 있던 실행에서만 큰 오차가 나오는 패턴.
**반증**: 동일 raw 파일로 offset을 인위적으로 −0.3s 주고 병합해 결과가 변하는지 확인. 변하지 않으면 H4 확정.

##### H5. 세그먼트 concat이 "영상 세그먼트 길이 == trim된 오디오 세그먼트 길이"를 가정한다 (확신도: 중간)
`_concat_screen_sys_segments`는 각 세그먼트 앞부분만 trim하고 꼬리는 손대지 않은 뒤 이어붙인다(`recorder.py:1195-1201`). 영상 세그먼트 길이와 오디오 세그먼트 길이의 잔차가 매 세그먼트마다 **누적**되므로, N번 pause한 녹화의 마지막 세그먼트 오차는 앞 세그먼트 오차의 합이다.

##### H6. PTS→wall 변환 자체의 오차 (확신도: 낮음 — 크기가 안 맞음)
`_sample_buffer_wall_clock`은 `mach_absolute_time()`과 `time.time()`을 비원자적으로 두 번 읽고(`system_audio.py:170-171`) `pts.epoch`를 무시한다(`:166`). 전자는 선점 시 수 ms, 후자는 epoch≠0일 때만 문제. **316ms를 설명하지 못한다.**

##### H7. 프로브 정합 필터의 펄스 오정렬 (확신도: 낮음)
펄스 간격이 80ms, 전체 스팬 160ms(`sync_diagnostics.py:23-27`)이므로 오정렬 오차는 80/160/240/320ms로 **양자화**된다. 316ms ≈ 320ms는 우연이라기엔 가깝다. 반증: 해당 세션 `session.json`의 정합 점수 분포와 차순위 피크 위치를 확인.

#### 4. 근본 해결책 권고

##### 1순위: 앵커를 전부 host clock PTS로 통일 — `time.time()`을 싱크 계산에서 완전히 제거
**왜 근본적인가**: offset의 정의가 `PTS_video_first − PTS_audio_first`가 되면 시계 변환도, 콜백 지연도, wall clock 슬루도 식에서 사라진다. 두 개의 SCStream을 유지해도 무방하다 — 둘 다 host clock에 있으므로 PTS끼리는 직접 비교 가능하다(이게 두 스트림이 "같은 원점을 공유한다"의 정확한 의미다).
**구현 스케치**:
1. `ScreenCaptureKitRecordingDriver`에 `SCStreamOutputTypeScreen` 출력을 **추가로** 등록한다(SCRecordingOutput은 그대로 둔다). 콜백에서 첫 프레임의 PTS와 `SCStreamFrameInfoDisplayTime`만 읽고 즉시 버린다(프레임 처리 비용 0에 가깝게 — 필요하면 `queueDepth` 최소).
2. `recordingDidStart` 시점 이후 도착한 첫 프레임의 PTS를 `video_anchor_host`로 확정. 동시에 그 프레임의 host time과 `recordingDidStart` wall time의 차를 로그로 남겨 H1을 지속 감시.
3. `system_audio.py`에 `first_sample_pts_host`(변환 없는 raw host seconds)를 추가 노출.
4. `offset = video_anchor_host − audio_anchor_host`. `_offset_from_anchor`(`recorder.py:365`)의 인자를 wall→host로 교체.
**이론적 정확도 상한**: 프레임 그리드 양자화 ±1/(2·30fps) = ±16.7ms, 오디오 버퍼 내 샘플 단위 보정 가능(=0.02ms). 즉 **~17ms**.
**리스크**: 스크린 출력을 추가하면 SCStream 처리 부하가 늘고, `SCRecordingOutput`이 기록한 첫 프레임이 우리가 본 첫 프레임과 동일 프레임인지 보장이 없다(문서 미공개). 완화: 두 값의 차를 매번 로그로 남겨 통계적으로 검증.

##### 2순위(더 근본, 비용 큼): SCRecordingOutput 폐기 + 단일 SCStream + AVAssetWriter
영상 프레임과 오디오 버퍼를 **같은 스트림**에서 받아 `AVAssetWriter`에 각 버퍼의 PTS로 그대로 append한다. 오디오는 popping을 피하기 위해 `AVAssetWriterInput`을 PCM/ALAC로 두거나, 지금처럼 WAV로 빼고 PTS만 기록한다. offset 계산이라는 개념 자체가 사라진다(`-ss`/`-itsoffset`/`amix` 정렬 문제 전부 소멸).
**정확도 상한**: 샘플 정확(±1 오디오 프레임) + 프레임 양자화 ±16.7ms.
**리스크**: v1.1.13에서 회피했던 오디오 인코딩 popping을 다시 만날 수 있음 — 단 그건 인코더 문제이므로 PCM append로 우회 가능. 구현량이 가장 크다.

##### 3순위(1순위와 병행 필수): PTS 보존 + gap/drift 보정
`_PCMFileWriter`에 "기대 PTS"를 유지하고, 도착 버퍼 PTS가 기대치보다 앞서면 잘라내고 뒤처지면 **차이만큼 무음을 삽입**한다(Nonstrict가 권하는 표준 처방). 종료 시 `(pts_last − pts_first)` vs `총샘플/48000`로 실효 샘플레이트를 산출해 `aresample=async=...` 또는 `asetrate`로 최종 보정. 마이크는 별 디바이스 클럭이므로 sys와 **각각** 보정한다.
**정확도 상한**: 장시간 drift를 ppm 수준(1시간에 <10ms)으로 억제.

##### 4순위: 병합 경로의 정직화
- `-itsoffset`을 amix 경로에서 **금지**하고 `adelay=<ms>|<ms>`로 교체(amix가 PTS를 무시하므로 필터 내부에서 무음을 넣어야 한다).
- `_trim_wav`의 음수 offset 분기를 추가: `anullsrc` 무음을 |offset|만큼 prepend 후 concat.
- `±50ms` 데드존 제거(`recorder.py:310-313`), 소수점 3자리 대신 샘플 수로 지정.
- AAC 프라이밍(1024~2112 샘플)을 상수로 보정하거나 `-c:a` 유지 시 `elst` 기록 여부를 명시적으로 확인.

##### 5순위: 진단 프로브의 기준 교정 (H2 대응)
`kAudioDevicePropertyLatency` + `kAudioDevicePropertySafetyOffset` + 스트림 latency를 조회해 `click_started_at`에 더하고, 세션 메타에 출력 디바이스 이름/전송(BT 여부)을 기록한다. 이걸 안 하면 프로브는 **상대 재현성(0.3ms)은 좋지만 절대 정확도가 L만큼 편향**된 계측기로 남는다.

#### 5. 다른 관점에서 나올 반론과 예비 답변

**"final 편차가 +17ms/−32ms면 이미 허용치(80ms) 안이다. 과잉 설계 아닌가?"**
— 두 값은 서로 다른 오차원이 **우연히 상쇄된** 결과일 수 있다. H2(출력 지연 L, +150~300ms 가능)와 H4(음수 offset no-op)가 동시에 살아 있으면 부호가 반대인 두 큰 항이 상쇄되어 작은 잔차를 만든다. −409ms 이상치가 바로 그 상쇄가 깨진 실행이다. 상수항을 하나씩 제거해 잔차의 **분산**을 줄이는 것이 목표여야 한다.

**"두 개의 SCStream이라서 문제다. 하나로 합치면 끝난다"(구조 관점)**
— 방향은 동의하지만, 스트림 개수는 근본 원인이 아니다. 두 스트림의 PTS가 같은 host clock에 있는 한 PTS끼리 비교하면 정확하다. 실제 원인은 **영상 쪽 PTS를 관측할 수단을 SCRecordingOutput 채택으로 스스로 포기한 것**이다. 스트림을 하나로 합쳐도 SCRecordingOutput을 계속 쓰면 영상 앵커는 여전히 `time.time()` 콜백이고 오차 구조는 그대로다.

**"ffmpeg offset 대신 처음부터 하나의 mp4로 뽑으면 된다"(파이프라인 관점)**
— 2순위 안과 동일한 결론이다. 다만 popping 회귀 리스크가 있으니 1순위(앵커 통일)를 먼저 넣어 즉시 이득을 취하고, 2순위는 별도로 검증하며 이행하는 순서를 권한다.

**"drift는 몇 분짜리 회의에서 무의미하다"**
— 100ppm 기준 10분에 60ms로 이미 허용치의 3/4을 먹는다. 더 중요한 건 drift가 아니라 **탭 버퍼 드롭**이다. 이건 크기가 예측 불가하고(한 버퍼 = 수십 ms) 현재 코드에 감지 수단이 전혀 없다(`system_audio.py:461-470`). 무해함을 주장하려면 먼저 관측 가능하게 만들어야 한다.

**"PTS 앵커가 316ms 이르게 나왔으니 PTS를 믿을 수 없다"**
— 부호가 이론이 예측하는 방향과 일치한다(탭은 DAC 앞단이므로 PTS 기준은 청취 기준보다 항상 앞선다). 즉 이 316ms는 PTS의 오류가 아니라 **프로브가 측정하지 못한 출력 지연 L의 정체**일 가능성이 가장 크며, 그렇다면 "PTS를 버릴 이유"가 아니라 "L을 상수로 보정할 이유"다. 판별 방법은 §3 H2의 반증 절차로 명확히 갈린다.

---

**Sources**: [SCStreamFrameInfoDisplayTime / ScreenCaptureKit 캡처 가이드](https://developer.apple.com/documentation/ScreenCaptureKit/capturing-screen-content-in-macos), [Nonstrict — Handling audio capture gaps on macOS](https://nonstrict.eu/blog/2024/handling-audio-capture-gaps-on-macos/), [FFmpeg af_amix.c 소스](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_amix.c), [AVAudioPlayer.deviceCurrentTime](https://developer.apple.com/documentation/avfaudio/avaudioplayer/1387462-devicecurrenttime?language=objc), [FFmpeg Filters (adelay)](https://ffmpeg.org/ffmpeg-filters.html)


## [1R] 관점 B — 구현 감사

### 관점 B: 구현 감사

#### 1. 핵심 주장 (3문장 이내)

앵커 계산은 이 문제의 **일부**일 뿐이고, 진짜 위험은 **WAV 타임라인 자체가 wall clock 과 무관하게 늘어나고 줄어든다**는 점이다 — `_PCMFileWriter`는 버퍼 간 gap 을 채우지도, PTS 와 샘플 수를 대조하지도, 드롭을 감지하지도 않으며, 콜백 안 예외는 조용히 삼켜져 버퍼 단위로 파일이 짧아진다(system_audio.py:387-396, 483-485). 여기에 offset 적용 경로가 **`-ss`(양수)만 실제로 동작하고 `-itsoffset`(음수)는 amix 안에서 사실상 무시**되며(recorder.py:309-314), 다중 세그먼트 경로는 첫 세그먼트 offset 을 **두 번** 적용한다(recorder.py:936, 948). 결론적으로 "두 개의 독립 스트림 + wall clock 앵커 + 사후 ffmpeg 이동" 구조는 **오차를 원리적으로 ±수십 ms 이하로 못 내리는 구조**이므로, 영상 t=0 을 파일 밖 wall clock 이 아니라 **같은 host clock 의 샘플버퍼 PTS**로 얻는 방향으로 구조를 바꿔야 한다.

#### 2. 발견한 결함 목록

**D1. WAV 에 gap 보정·샘플 회계·드롭 감지가 전무 (심각도: 치명)**
`system_audio.py:387-396`. `write_sample_buffer`는 `pcm_bytes`를 append 하고 `_data_bytes`만 누적한다. 기대 샘플 위치를 PTS 로 계산해 비교하는 코드가 없고, 누락분을 무음으로 채우는 코드도 없다. 버퍼 1개(보통 1024 sample ≈ 21ms)가 유실될 때마다 **그 이후 전체 오디오가 21ms 앞으로 당겨진다(타임라인 압축)**. 발현 조건: 델리게이트 큐 오버런, GIL 경합, 디스크 지연, 포맷 변경. → **실측 사실 4(PTS 앵커가 실측 대비 316ms "이르게" 나옴)와 5(-409ms)를 그대로 설명한다.** 프로브 클릭 전에 15개 버퍼가 빠지면 클릭이 WAV 안에서 315ms 앞에 나타나고, 진단은 이것을 "앵커가 316ms 이르다"로 오독한다 — 두 원인이 진단 지표상 **구분 불가**다.

**D2. 콜백 예외 삼킴 = 무보고 샘플 손실 (심각도: 치명)**
`system_audio.py:483-485`. `_copy_pcm_bytes` 의 `RuntimeError`, 포맷 조회 실패, 포인터 정규화 실패 전부 `_flog` + `logger.debug` 로만 남고 그 버퍼는 사라진다. 파일 로그(`~/Library/Logs/...audio.log`)를 열지 않으면 아무도 모른다. D1 과 결합해 임의 길이의 타임라인 압축을 만든다.

**D3. 앵커가 파일에 들어가지 않은 샘플을 가리킬 수 있다 (심각도: 높음)**
`system_audio.py:472-481`. `first_sample_host_at` 은 **write 시도 전에** 설정된다. 첫 버퍼(들)의 write 가 실패하면 앵커는 WAV 에 없는 샘플의 시각을 가리키고, offset 은 그만큼 과대해져 오디오가 앞으로 당겨진다. → 사실 4·5.

**D4. 순수 Python de-interleave 루프가 델리게이트 큐를 막는다 (심각도: 높음, D1 의 원인)**
`system_audio.py:330-337`. `num_samples × channels` 회 Python 슬라이스 대입. 48kHz stereo 에서 초당 ~10만 회, 여기에 `self._lock`(system + microphone 두 output 이 **공유**, :480-481)과 GIL 이 겹친다. `addStreamOutput_type_sampleHandlerQueue_error_` 에 큐를 `None` 으로 넘겨(:589-594) ScreenCaptureKit 내부 큐에서 실행되므로, 핸들러가 늦으면 SCStream 은 버퍼를 **버린다**. 마이크까지 켜면 부하가 2배 + 직렬화. → 사실 3(backlog burst)·4·5.

**D5. 첫 버퍼로 포맷 확정, 이후 변경 무대응 (심각도: 높음)**
`system_audio.py:391`(`if self._format is None`) + `367-385`(close 시 첫 포맷으로 헤더 기록). 녹화 중 출력 디바이스 전환(AirPods 연결 등)으로 sample rate 가 48k→44.1k 로 바뀌면: (a) 헤더는 48k 인데 내용은 44.1k → **재생 길이가 8.8% 늘어나는 선형 드리프트**(1시간이면 5분), (b) 채널/폭이 바뀌면 `CMSampleBufferCopyPCMDataIntoAudioBufferList` 가 err 를 반환해 D2 경로로 전 버퍼 유실. 사후 offset 이동으로는 **원리적으로 복구 불가**(스칼라 이동 vs 시간축 스케일). → 사실 5의 실행 간 편차(+17/-32/-409ms)가 특정 실행에서만 크게 튀는 패턴과 정합.

**D6. `-itsoffset` 음수 경로가 amix 안에서 무효 (심각도: 높음)**
`recorder.py:309-314` + `1611-1624`, `1470-1483`. ffmpeg `af_amix` 는 입력별 FIFO 에서 사용 가능한 샘플을 그대로 합산하며 입력 PTS 로 정렬하지 않는다. 즉 오디오가 영상보다 **늦게** 시작한 경우(offset<0) 앞을 무음 패딩하지 않고 그냥 0 시점부터 섞어버려 **보정이 통째로 사라진다**. 게다가 `has_sys and not has_mic` 분기(:1639-1649)는 필터 없이 `-map` 이라 `-itsoffset` 이 mp4 edit list 로 들어가 **동작한다** → 마이크 유무에 따라 같은 offset 이 다르게 해석되는 비대칭. 발현: 마이크가 SCStream 보다 늦게 붙는 경우, resume 직후.

**D7. 다중 세그먼트에서 첫 세그먼트 offset 이중 적용 (심각도: 높음)**
`recorder.py:1195-1201`(모든 세그먼트를 `_trim_wav` 로 이미 잘라 concat) 후 `recorder.py:936` `first_sys_offset = sys_segments[0][1]`, `:948` `first_mic_offset = ...[0][1]` 을 그대로 반환 → `merge_audio_into_mp4` 에서 `-ss` 로 **한 번 더** 자른다. 같은 파일의 `_concat_segments` 는 올바르게 `0.0, 0.0` 을 반환한다(:1309-1310) — 두 경로가 모순. pause/resume 을 한 번이라도 쓰면 오디오가 offset(통상 0.1~1.0s)만큼 추가로 앞당겨진다.

**D8. 화면 다중 세그먼트 영상이 PTS 정규화 없이 `-c copy` concat (심각도: 높음)**
`continuous_screen_recorder.py:196-212`(`finalize_mp4_segments`). 그런데 `recorder.py:1080-1088` 은 "SCRecordingOutput 은 Mach time 기반 PTS 를 기록해 `-c copy` 결합 시 일시정지 시간만큼 frozen 구간이 생긴다"고 명시하고 `_concat_videos_normalized` 를 만들어 뒀다 — 그러나 이 함수는 `_concat_segments`(screen 분기)에서만 호출되고, `stop()` 의 screen 분기는 `_concat_segments` 를 **호출하지 않는다**(:900-953). 즉 `recorder.py:1262-1311` 과 `_concat_videos_normalized` 는 **전부 죽은 코드**이고, 영상은 pause 구간을 포함한 채, 오디오는 pause 구간이 제거된 채 합쳐진다 → pause 당 수 초 단위 desync.

**D9. pause 시 영상이 오디오보다 오래 녹화된다 (심각도: 높음)**
`recorder.py:730-751`: sys 오디오 stop → `_stop_mic()`(ffmpeg `q` 전송 후 **최대 10초** wait, :508) → 그 다음에야 `_screen_recorder.pause()`. 세그먼트 tail 이 영상 쪽만 길어지고, 이 tail 불일치를 보정하는 코드는 어디에도 없다(offset 은 head 만 다룬다). resume 은 반대로 오디오를 먼저 시작(:786) 후 영상(:795)이라 head 는 offset 으로 흡수되지만, tail 결손은 세그먼트마다 **누적**된다.

**D10. 마이크 세그먼트 목록이 영상 세그먼트와 정렬 보장 없음 (심각도: 중)**
`recorder.py:744-745`, `916-917`: `_mic_path is not None` 일 때만 `_screen_mic_segments` 에 append. 어느 resume 에서 마이크 시작이 실패하면 그 세그먼트만 빠지고, 나머지가 back-to-back 으로 붙어 **그 세그먼트 길이만큼 이후 마이크가 앞으로 당겨진다**. 게다가 sys 는 `self._segments`, mic 는 `_screen_mic_segments` 로 이중 장부.

**D11. dead zone 과 반올림 (심각도: 중)**
`recorder.py:310-313`(`> 0.05` / `< -0.05`), `1009`(`offset <= 0.05` → 그냥 copy). 최대 50ms 를 조용히 버린다. 사실 2(recordingDidStart 가 pts 0 보다 40~90ms 이름)의 계통 편향과 같은 자릿수라 **둘이 겹치면 상쇄되기도 하고 배가되기도 한다** → 사실 5의 +17ms / -32ms 를 설명하기 충분. `%.3f` 반올림은 1ms 이하로 무해.

**D12. 앵커 폴백 사다리가 ±1.5s 오차값을 조용히 채택 (심각도: 중)**
`recorder.py:35-36`, `291-297`, `573-579`. `first_sample_host_at` 실패 시 `first_sample_at`(사실 3: 진짜보다 0.08~1.5s **늦음** → 오디오가 늦게 배치) 또는 `started_at`(진짜보다 이름 → 오디오가 이르게 배치)로 폴백한다. 경고 한 줄(:574) 외에 클램프도, 보정 포기도 없다.

**D13. PTS 도메인 검증 부재 (심각도: 중)**
`system_audio.py:158-175`. `wall_now - (host_now - pts_seconds)` 는 PTS 가 host clock 도메인이라는 **가정**에 전적으로 의존한다. `SCStreamOutputTypeMicrophone` 의 PTS 도메인은 검증되지 않았고(오디오 디바이스 클럭일 수 있음), sanity check(`0 <= host_now - pts <= 5`)가 없다. → 사실 4의 316ms 가 sys/mic 도메인 차이일 가능성.

**D14. `stop()` 이 in-flight 버퍼를 버려 tail 을 자른다 (심각도: 중)**
`system_athudio.py:649-656`: `stopCaptureWithCompletionHandler_(None)` (completion 대기 없음) 직후 `closeFiles()`. 남은 버퍼는 `writer.is_open == False` 로 조용히 버려진다. D9 와 합쳐 세그먼트 길이 불일치를 키운다.

**D15. 보정값 학습 피드백 루프 오염 (심각도: 중)**
`app.py:799` 는 runtime 에 **config 값**을 기록하는데, `recorder.py:371-373` 은 `_using_stream_microphone` 이면 그 값을 **적용하지 않는다**. `sync_diagnostics.py:711-732` 은 "기록된 값이 적용됐다"고 가정해 `correction = 적용값 + 잔차` 를 계산하므로, SCStream 마이크 세션에서 학습한 보정값이 ffmpeg 폴백 세션에 잘못 적용된다. `app.py:820-845` 는 mode(screen/audio) 구분 없이 최신 세션에서 갱신한다.

**D16. `compress_and_merge` 의 무라벨 amix (심각도: 낮음~중)**
`recorder.py:1477`: 입력 3개인데 `amix=inputs=2` 에 라벨이 없어 "미사용 오디오 스트림 앞 2개"가 자동 선택된다. `mov_path` 에 오디오가 있으면(capture_audio=True 경로) mic 대신 mov 오디오가 섞여 offset 체계가 붕괴한다.

**D17. AEC/후처리는 타임라인 안전 (참고)**
`acoustic_echo_cancel.py:205-214` 는 mic 원본 길이·시작점을 보존하고 부호 규약도 recorder(:1446)와 정합. `audio_preprocessor.py` 는 STT 전용(`pipeline.py:167`)이라 mp4 트랙에 영향 없음. 다만 `compress_and_merge` 의 `mic_echo_cancel` 경로는 app 이 screen 모드에서 `merge_audio_into_mp4` 만 호출(:1521-1530)하므로 **실행되지 않는다**(사실상 죽은 기능).

#### 3. 근본 원인 가설 (순위별)

**H1. WAV 타임라인 압축(D1+D2+D3+D4)이 잔차와 실행 간 분산의 주원인. 확신도 高.**
근거: 사실 4·5 는 "앵커 오차"로는 부호가 일관돼야 하는데 +17/-32/-409 로 분산이 크다. 스칼라 앵커 오차 모델은 이 분산을 못 만들고, 버퍼 유실 모델은 정확히 이런 무작위·단측(항상 오디오가 앞으로) 분포를 만든다. 반증법: 델리게이트에서 `expected_samples = (pts - first_pts) * rate` 와 `_data_bytes/frame` 을 매 버퍼 비교해 로그 → 차이가 0 이면 H1 기각.

**H2. 앵커의 계통 편향(사실 2의 -40~-90ms) + dead zone(D11). 확신도 高(크기 작음).**
근거: recordingDidStart 는 첫 프레임 write 보다 이르다. 사실 5의 ±30ms 급 잔차는 이것만으로 설명된다. 반증법: 상수 -0.065s 를 더해 dead zone 을 0.005 로 낮췄을 때 잔차가 0 근처로 수축하는지.

**H3. pause/resume 경로의 이중 trim(D7) + `-c copy` concat(D8) + tail 결손(D9). 확신도 高.**
근거: 코드 상 명백하며 서로 독립적으로 누적. 반증법: 2-세그먼트 녹화에서 최종 mp4 길이 vs Σ세그먼트 길이, 그리고 세그먼트별 클릭 위치 측정.

**H4. 음수 offset 무보정(D6). 확신도 中.**
반증법: 합성 WAV 2개(하나에 500ms 지연 클릭)로 `-itsoffset`+amix vs `adelay` 결과 비교.

**H5. 포맷 전환 드리프트(D5). 확신도 中(발생 시 치명).** 반증법: `_flog` 에 버퍼별 ASBD 를 남기고 AirPods 연결 시나리오 재현.

#### 4. 근본 해결책 권고

**1순위 — 영상 t=0 을 wall clock 이 아니라 같은 host clock PTS 로 얻고, 오디오 타임라인을 PTS 로 회계한다.**
왜 근본적인가: 현재 구조의 결함은 "두 스트림"이 아니라 **영상 t=0 이 파일 밖에서만 추정된다**는 점이다(SCRecordingOutput 은 PTS 를 노출하지 않고, 콜백 시각·startCapture 시각은 사실 1·2 처럼 수십~1000ms 편향). 두 t=0 을 같은 클럭의 PTS 로 얻으면 offset 은 계산이 아니라 **측정**이 되고, 앵커 계측 코드 전부가 사라진다.
구현 스케치:
1. `ScreenCaptureKitRecordingDriver` 에 `SCStreamOutputTypeScreen` output 을 하나 더 붙인다(픽셀 미사용, `CMSampleBufferGetPresentationTimeStamp` 만 읽고 즉시 return). 세그먼트 시작 후 **첫 프레임 PTS**를 `handle.first_frame_pts` 에 기록. 프레임 콜백이 SCRecordingOutput 의 mp4 pts 0 과 같은 프레임인지 1회 캘리브레이션(플래시 프로브)으로 확인하고, 남는 계통 편향은 상수로 흡수.
2. `_PCMFileWriter` 에 `_first_pts`/`_expected_samples` 를 추가: 매 버퍼에서 `gap = round((pts - first_pts) * rate) - written_samples`; `gap > 0` 이면 **무음 패딩**, `gap < -tolerance` 면 겹침만큼 절단, 로그+카운터. 포맷이 바뀌면 파일을 닫고 새 파일을 열어 세그먼트로 취급(또는 resample). 이것만으로 D1·D2·D3·D5 가 동시에 닫힌다.
3. offset 을 **샘플 수**로 계산해 `atrim=start_sample=N` / `adelay=Nms:all=1` (+`apad`) 로 적용하고 `-ss`/`-itsoffset`/dead zone 을 제거한다. 음수 offset 은 반드시 `adelay` 로 앞을 무음 패딩. `amix` 대신 `adelay→amix=normalize=0` + `alimiter` 로 레벨도 결정적으로.
4. 세그먼트 경로: `_trim_wav` 로 이미 자른 뒤에는 offset 을 **0.0 으로 반환**(D7 즉시 수정, 1줄), 영상은 `_concat_videos_normalized` 를 실제로 호출하도록 `finalize_mp4_segments` 를 교체(D8), 세그먼트마다 `video_segment_duration` 을 재고 오디오를 `apad`/절단으로 **길이 일치**시킨 뒤 concat(D9·D10). pause 순서를 "영상 pause → 오디오 stop" 으로 뒤집는다.
회귀 위험: 프레임 output 추가로 D4 부하가 늘 수 있으므로 첫 프레임 이후 output 을 제거하거나 즉시 return. `adelay` 전환은 기존 테스트(`tests/test_capture_sync.py:210`)가 `-ss`/`-itsoffset` 인자를 검증하므로 함께 갱신 필요.
작업량: 2~3일 (2·3·4 만 하면 1일, 그것만으로도 D1·D6·D7·D8·D9 가 닫힌다 — **먼저 이것부터**).

**대안 A — SCRecordingOutput 폐기, AVAssetWriter 직접 기록.** 영상·오디오를 같은 PTS 타임라인으로 한 writer 에 append 하면 싱크가 **구조적으로 0**이 되고 pause/resume 은 "append 중단"으로 끝나며 사후 ffmpeg 이동이 전부 사라진다. 근본성 최고. 위험: 인코더 설정·성능·디스크 스루풋을 직접 책임져야 함(popping 이슈 재발 가능). 작업량 1~2주.

**대안 B — 매 실행 프로브 자동 캘리브레이션.** 녹화 시작 시 비가청 신호를 쏴 최종 파일에서 잔차를 측정·보정. 원인을 고치지 않고 증상만 덮으며, D1 같은 **시간축 스케일 오류에는 무효**(스칼라 보정으로 복구 불가). 최후 수단.

#### 5. 예상 반론과 예비 답변

- **"버퍼 유실은 추측이다."** 그렇다. 그래서 2번 항목의 샘플 회계는 **수정과 계측을 동시에** 한다(패딩하면서 카운터를 남긴다). 카운터가 0 이면 H1 은 즉시 기각되고 잃는 것은 없다.
- **"앵커만 잘 잡으면 된다(관점 A)."** 앵커는 스칼라다. 사실 5의 부호가 뒤섞인 ±수백 ms 분산과 계통 편향을 동시에 만들 수 없다. 최소한 D7(이중 trim)·D8(`-c copy`)·D6(음수 무효)은 앵커와 무관한 확정적 버그이며, 이것들을 남긴 채 앵커를 튜닝하면 **버그를 앵커로 상쇄하는** 값이 학습된다(D15 의 피드백 루프가 이미 그 위험을 실현하고 있다).
- **"두 스트림 구조가 원인이니 하나로 합쳐라."** 합치면 좋지만(대안 A), capture_audio=True 는 popping 때문에 폐기된 경로다(`recorder.py:523-525`). 두 스트림이어도 **같은 host clock PTS** 를 쓰면 오차는 프레임 1개 이하다 — 문제는 스트림 개수가 아니라 wall clock 경유다.
- **"pause/resume 은 드문 경로다."** D7·D8·D9 는 pause 를 한 번 쓰면 초 단위로 틀어진다. 드물다면 더더욱 조용히 방치돼 왔다는 뜻이고, `_concat_videos_normalized` 가 죽은 코드로 남아 있는 것이 그 증거다.


## [1R] 관점 C — 관측가능성·설계 대안

### 관점 C: 관측가능성과 설계 대안

#### 1. 핵심 주장 (3문장 이내)

필요한 양(`mp4 video pts 0`의 실시각 − `WAV sample 0`의 실시각)은 **원리적으로 관측 불가능한 것이 아니라, 현재 아키텍처가 관측 경로를 스스로 버려서 관측 불가능해진 것**이다 — 영상 쪽은 `capture_audio=False` + 프레임 콜백 없음이라 프로세스 안에서 비디오 sample buffer PTS를 단 한 번도 보지 않고(`continuous_screen_recorder.py:341-370`), 대신 델리게이트 콜백의 `time.time()`(`continuous_screen_recorder.py:424,430`)이라는 대리변수로 4개 이상의 미관측 잠재변수를 통째로 삼킨다. 진단 프로브는 이 잠재변수들과 스피커/디스플레이 지연이 선형결합으로 얽혀 있어 **미지수 4개에 방정식 1개**인 구조적 미식별(unidentifiable) 계측기이므로, 프로브로 앵커를 보정하는 루프는 원리적으로 수렴하지 않는다. 따라서 근본 해결책은 "추정을 더 잘하기"가 아니라 **영상·오디오를 같은 시계(CMClock host time)의 PTS 차이로 환원해 wall clock을 계산에서 완전히 제거하는 것**이며, 이는 대규모 재작성 없이 두 곳의 소규모 변경으로 달성된다.

#### 2. 관측가능성 분석

##### 필요한 양
$$\Delta = T_{\text{real}}(\text{video pts }0) - T_{\text{real}}(\text{WAV sample }0)$$

##### 현재 관측되는 양 (4개)
| 기호 | 코드 | 정체 |
|---|---|---|
| A | `continuous_screen_recorder.py:424` | `startCapture` 완료 핸들러의 `time.time()` |
| B | `continuous_screen_recorder.py:430` | `recordingOutputDidStartRecording` 콜백의 `time.time()` |
| C | `system_audio.py:473` | 첫 오디오 콜백 도착 `time.time()` |
| D | `system_audio.py:476` → `:158-175` | 첫 샘플 PTS(host clock)를 wall로 환산 |

현재 계산은 `Δ̂ = B − D` (`recorder.py:341-362`, `:615`).

##### 관측 불가 잠재변수
**영상 측 (3개, 모두 미관측):**
- **V1 = pts0 − B.** `recordingDidStart`는 *미디어 타임스탬프가 아닌 이벤트 알림*이고 dispatch queue를 한 번 건넌다. 더 나쁜 것은 `ContinuousCaptureController.start()`가 `start_stream()` → `start_segment()` 순서로 동작해 **recording output을 `startCapture` 이후에 추가**한다는 점(`continuous_screen_recorder.py:92-95`). Apple 문서는 "첫 샘플이 파일에 기록되는 것을 보장하려면 `addRecordingOutput`을 `startCapture` **이전에**"라고 명시한다. 즉 V1이 0.14~1.09초까지 벌어지는 실측(사실 1)은 버그가 아니라 이 순서의 필연적 결과다.
- **V2 = 컴포지팅/프레임 양자화.** `minimumFrameInterval = 1/30`(`:317`)이므로 첫 프레임의 PTS와 "화면이 실제로 그 모습이던 시각"의 관계는 최대 33ms 양자화된다. SCStream 프레임의 `SCStreamFrameInfoDisplayTime` 어태치먼트가 이 정보를 담고 있으나 코드는 프레임을 받지 않아 읽을 수 없다.
- **V3 = 컨테이너 타임라인 오프셋.** mp4 트랙 `start_time`/edit list, 그리고 다중 세그먼트 시 `setpts=PTS-STARTPTS`(`recorder.py:1096`)가 세그먼트별로 타임라인을 재기점화하는 것 — 오디오는 세그먼트별 개별 offset으로 trim 후 concat되므로(`recorder.py:1195-1201`) 세그먼트 경계마다 새 잠재변수가 추가된다.

**오디오 측 (2개):**
- **A1 = 이중 시계 비원자적 독출.** `_host_time_seconds()`와 `time.time()`을 연속 호출(`system_audio.py:170-172`). 통상 µs지만 Python 콜백 스레드에서 GIL 경합 시 ms급. 사실 4의 "316ms 이르게" 편차를 A1으로 설명할 수는 없다 — 그 크기는 V1/V2에서 온다.
- **A2 = τ_tap.** 탭 PTS가 "출력 디바이스에 렌더될 시각"인지 "믹스 버퍼가 생성된 시각"인지 검증된 바 없는 미지 상수.

따라서 실제 오차 = **V1 + V2 + V3 − A1 − A2**. 최소 4개의 미관측량 합이며, 이 중 어느 하나도 산출물이나 로그로부터 분리할 수 없다.

##### 정확도 상한
- D는 유일하게 잘 관측된 양이다(오차 ~수 ms). 문제는 전적으로 영상 측이다.
- B를 앵커로 쓰는 방식의 **최선 정확도**: V1의 dispatch 지터(수 ms~수십 ms) + V2의 프레임 양자화(33ms, 평균 편향 +16.5ms) ⇒ **최선 ±40~60ms, 상한은 존재하지 않음**. 델리게이트 콜백 지연을 위에서 묶어주는 메커니즘이 프레임워크에 없기 때문이다. 사실 6의 −409ms는 정규 분포의 꼬리가 아니라 모델 붕괴다.
- 여기에 코드가 스스로 만든 **±50ms 사각지대**가 얹힌다: `_audio_input_args`는 `|offset| ≤ 0.05`면 보정을 아예 적용하지 않는다(`recorder.py:309-314`). 허용치가 80ms인 시스템에서 50ms를 무조건 버리는 것은 오차 예산의 62%를 설계적으로 포기하는 것이다. `_trim_wav`가 `-ss ... -c copy`(`recorder.py:1013`)를 쓰는 것도 WAV 패킷 경계 양자화를 추가한다.
- **장시간 녹화에서는 상한 논의 자체가 무의미해진다.** `_PCMFileWriter.write_sample_buffer`는 PTS를 무시하고 바이트를 append하며(`system_audio.py:387-396`), WAV 헤더의 sample rate는 ASBD 공칭값(`:376-383`)이다. 즉 WAV의 시간축 = 샘플수/48000. 디바이스 클럭이 ε ppm 어긋나면 시간축이 **10~100ppm = 36~360ms/시간**으로 밀리고, 부하로 탭 버퍼가 드롭되면 그 지점에서 타임라인이 계단식으로 **짧아진다**(무음이 삽입되지 않으므로 이후 전 구간이 앞으로 당겨진다). 두 현상 모두 현재 구조에서 완전 미관측이다. 지금까지의 모든 실측은 수초짜리 프로브 녹화였으므로 이 항이 보이지 않았을 뿐이다.

##### 후처리에 개입 여지가 있는가
없다. `pipeline.py:150-157`은 최종 mp4를 받아 `extract_audio`로 16kHz mono를 뽑을 뿐이고(`audio_extractor.py:67-80`), 싱크를 만질 수 있는 마지막 지점은 `compress_and_merge`/`merge_audio_into_mp4`(`recorder.py:1466-1468`, `:1608-1610`)다. **싱크는 캡처 시점에 결정되고 병합 시점에 고정된다** — 파이프라인은 관측도 교정도 하지 못한다.

#### 3. 진단 프로브의 타당성 평가

프로브가 측정하는 양을 전개하면:

$$m_{sys} = \underbrace{[t_{sched} + \tau_{tap}]}_{\text{탭에 찍힌 클릭}} - \underbrace{[t_{alpha} + \delta_{disp} + q_{frame}]}_{\text{mp4에 찍힌 플래시}}$$

- `t_sched`: `deviceCurrentTime + lead`를 wall로 환산한 값(`sync_diagnostics.py:266-274`). `deviceCurrentTime`은 문서상 **출력 디바이스 클럭(host time base)** 이므로 예약은 정확하다 — 사실 5의 0.3ms 재현성은 이 때문이다.
- `δ_disp`: `setAlphaValue_(1.0)` → 실제 컴포지팅. AppKit/CoreAnimation은 **현재 run loop 회차 끝의 CATransaction commit** 시점에 반영되고, 그 직후 `time.time()`을 찍는다(`sync_diagnostics.py:319-320`). 즉 flash 타임스탬프는 항상 **실제 발광보다 이르다**(1 컴포지터 프레임 + window server 왕복).
- `q_frame`: 30fps 캡처 양자화, 0~33ms의 **단측 편향**.
- `detect_video_flash`는 30fps 소스에 `fps=120`을 걸어(`sync_diagnostics.py:515`) 프레임을 복제하므로 분해능은 여전히 33ms다 — 120fps라는 숫자는 정밀도를 만들어내지 못한다.

`_probe_emission_skew`(`:565-574`)는 `click_started_at − flash_started_at`, 즉 **이미 알고 있는 부분만** 뺀다. τ_tap, δ_disp, q_frame은 그대로 남는다. **채널 1개, 미지수 4개(Δ, τ_tap, δ_disp, q_frame) ⇒ 구조적 미식별.** 프로브는 "진짜 싱크 오차"를 관측할 수 없다. 사실 6의 +17/−32ms는 참값이 아니라 잡음 바닥(±30ms 수준)과 구별되지 않는 값이다.

**스피커 지연은 sys 경로에서는 대체로 상쇄된다** (탭은 DAC 이전의 믹스를 잡으므로). 그러나 **마이크 경로에서는 절대 상쇄되지 않는다**: 프로브 클릭은 스피커 → 공기 → 마이크를 지나므로 `L_spk + L_air + L_mic`(내장 스피커/마이크 30~150ms, Bluetooth 100~300ms)가 그대로 실린다. 그런데 `recommend_sync_adjustments`는 바로 이 값을 `mic_latency_correction_seconds`로 산출하고 `0.05 ≤ |correction| ≤ 2.0`이면 채택한다(`sync_diagnostics.py:729-732`) — **L_spk가 정확히 사는 구간**이다. 그리고 `app.py:820-851`이 이를 `config.yaml`에 영구 기록한다. 즉 **계측기의 음향 왕복 편향이 실제 회의 오디오의 타임스탬프 보정값으로 승격된다.** 사람이 말할 때 마이크 경로에는 스피커 지연이 존재하지 않으므로, 이 보정은 실제 회의에서 마이크를 `L_spk + L_air`만큼 **과보정**한다. 코드가 stream mic일 때 이 보정을 건너뛰는 것(`recorder.py:371-373`)은 옳은 직관이지만, ffmpeg fallback 경로에는 그대로 적용된다.

**분리하려면 무엇이 필요한가**: (a) 외부 계측기 — 화면과 스피커를 한 대의 카메라로 동시 촬영해 *하나의 시계*에 담기, 또는 (b) 미지수를 우회하는 신호 경로 — 클릭을 재생 경로가 아닌 *캡처 경로*로 주입(가상 루프백)하고, 플래시 시각을 `time.time()`이 아닌 **비디오 sample buffer의 PTS/displayTime**에서 읽기. (b)는 소프트웨어만으로 가능하며, 그것을 구현한 순간 **프로브 자체가 불필요해진다** — PTS 차이를 직접 계산하면 되기 때문이다. 이것이 아래 대안 5의 논지다.

#### 4. 설계 대안 비교표 + 각 안 상세

| 안 | 이론적 정확도 상한 | 잠재변수 제거 | 작업량 | 회귀 위험 | drift(1h+) | 자기검증 |
|---|---|---|---|---|---|---|
| 1. 단일 SCStream, `capture_audio=True` | 프레임워크 내부 정렬 (<1프레임) | V1·V2·V3·A1·A2 전부 | 매우 작음 (플래그 1개) | **높음** (popping, mic 트랙 손상 보고) | 자동 해결 | 불가 (믿을 뿐) |
| 2. 참조 오디오 트랙 + 교차상관 | 코덱 프라이밍 상수 1개 (≤43ms, 1회 캘리브레이션) | 시변 잠재변수 전부 → 상수 1개 | 중간 (후처리 1패스) | 매우 낮음 (캡처 경로 무변경) | **측정·보정 가능** | **가능** |
| 3. AVAssetWriter 직접 mux | 정확 (샘플/프레임 단위) | 전부 | **매우 큼** | 높음 (프레임 드롭, 파일 무효화) | 자동 해결 | 부분 |
| 4. 현재 구조 + 앵커 정밀화 | ±40~60ms, 상한 없음 | 없음 | 작음 | 낮음 | **무대책** | 불가 |
| 5. 첫 프레임 PTS 관측 전용 stream output | host clock 차이 = 정확 | V1·V2·V3 (문서 보장 조건부) | **작음 (~40줄)** | 매우 낮음 | 별도 필요(5b) | 대안 2와 결합 시 가능 |
| 5b. `_PCMFileWriter` PTS 정렬 기록 | — | drift·드롭 제거 | 작음 (~15줄) | 낮음 | **해결** | 로그로 관측 |

**대안 1 상세.** `recorder.py:556`의 `capture_audio=False`를 되돌리기만 하면 Δ 개념 자체가 소멸한다 — AVFoundation 내부에서 같은 세션 타임라인에 mux되므로 wall clock이 계산에 등장하지 않는다. 주목할 점: popping 회피 근거는 이미 낡았을 가능성이 크다. `capture_audio=True` 경로에는 이후 `setSampleRate_(48000)` / `setChannelCount_(2)` 하드닝이 추가되어 있고(`continuous_screen_recorder.py:306-312`) 주석이 "오디오 안정성"이라 밝히고 있다 — 즉 popping의 유력 원인(포맷 네고시에이션)에 대한 수정이 *비활성 경로에* 들어 있다. 다만 실패 모드가 치명적이다: popping이 재현되면 최종 산출물이 손상되고 자동 검출 수단이 없다. 또한 Apple 개발자 포럼에는 **`captureMicrophone = true`일 때 SCRecordingOutput의 mp4가 손상·재생 불가가 된다**는 보고가 있어, 마이크까지 SCRecordingOutput에 맡기는 것은 현재 권장할 수 없다.

**대안 2 상세.** 참조 트랙과 sys.wav는 같은 탭 샘플에서 유래하므로 교차상관 피크는 이론상 샘플 단위로 정확하다. 남는 편향은 AAC 프라이밍 잔차 하나 — **런마다 변하지 않는 상수**이며 오프라인 결정론적 실험(임펄스 WAV → 같은 코덱 → 디코드 → 측정)으로 1회 캘리브레이션 가능하다. 즉 **시변 잠재변수 4개 → 런 불변 상수 1개**로의 질적 전환이다. 부가 가치가 크다: (i) 파일 전체를 N개 창으로 나눠 lag(t)를 선형 적합하면 **drift 기울기까지 측정**되어 `asetrate`로 교정할 수 있다, (ii) popping이 나는 트랙은 버려지므로 popping 위험이 산출물에서 분리된다 — 즉 대안 2는 **대안 1의 안전한 A/B 테스트 하네스**다, (iii) 프로브·스피커·디스플레이가 계측에서 완전히 빠진다. 실패 모드: 전 구간 완전 무음(그 경우 싱크가 무의미), 그리고 상관 피크가 여러 개인 주기적 신호(정규화 상관 + 탐색창 ±3초로 방어).

**대안 3 상세.** 원리적으로 가장 깨끗하다 — `startSessionAtSourceTime(firstVideoPTS)` 후 두 종류 버퍼를 각자 PTS로 append하면 정렬은 구성적으로 참이다. 그러나 PyObjC로 하기에는 위험이 크다: `system_audio.py:110-124`가 보여주듯 CMSampleBuffer를 다루려면 이미 ctypes 곡예가 필요하고, 4K@30 프레임마다 Python 브리지를 건너야 하며(GIL), `isReadyForMoreMediaData` 백프레셔·`finishWriting` 누락 시 파일 무효화·장시간 메모리 증가를 모두 직접 관리해야 한다. 현실적 변형은 **Swift 헬퍼 바이너리로 분리**하는 것이고, 이 레포에는 이미 `apple_speech_transcriber.swift` / `notify_sender.swift` 선례가 있다. 그래도 대안 5가 같은 관측성을 1/20의 작업량으로 주므로 지금 선택할 이유가 없다.

**대안 4 상세.** 통계적으로 불가능하다. 사실 6의 세 표본(+17, −32, −409ms)은 표준편차 ≈ 240ms, 평균의 표준오차 ≈ 139ms다. 허용치 80ms 안으로 편향을 확정하려면 n ≈ (240/40)² ≈ 36회가 필요하고, 그조차 오차가 정상(stationary)이라는 가정 하에서만 유효하다 — −409ms 이상치는 그 가정을 부정한다. 게다가 캘리브레이션의 유일한 기준자가 §3에서 미식별로 판정된 프로브다. **자기 참조 루프**이며, drift는 아예 다루지 못한다.

**대안 5 상세 (추천 핵심).** SCStream 하나에 **recording output과 stream output을 동시에** 붙일 수 있다(Apple 문서 확인 — 화면/오디오/마이크 stream output + recording output 공존). 따라서 `ScreenCaptureKitRecordingDriver`에 `SCStreamOutputTypeScreen` stream output을 붙여 **첫 프레임의 PTS(host clock)만 읽고 즉시 `removeStreamOutput`** 하면 된다. 그러면
$$\Delta = \text{firstVideoPTS}_{host} - \text{firstAudioPTS}_{host}$$
로 **두 값이 같은 시계에서** 나온다 — wall clock, 프로브, 스피커, 디스플레이가 전부 계산에서 사라진다. 유일한 잔여 가정은 "SCRecordingOutput의 pts 0 == 스트림에 전달된 첫 프레임"이며, 이는 **`addRecordingOutput`을 `startCapture` 이전에 호출하면 문서가 보장**한다. 즉 `continuous_screen_recorder.py:92-95`의 순서를 뒤집는 것이 이 안의 전제조건이자, 어떤 대안을 택하든 해야 할 수정이다. 비용: 프레임 콜백 1회분(≈0), 코드 ~40줄.

#### 5. 근본 해결책 권고

**1순위 (즉시): 대안 5 + 5b — "wall clock을 싱크 계산에서 완전히 축출"**
1. `addRecordingOutput`을 `startCapture` **이전에** 호출하도록 `ContinuousCaptureController.start()` 순서 변경 (`continuous_screen_recorder.py:92-95`). V1의 구조적 원인 제거.
2. 첫 프레임 PTS 관측 전용 `SCStreamOutputTypeScreen` output 추가 → `first_frame_host_at` 노출 → `_screen_video_anchor`(`recorder.py:341-362`)를 `B`가 아닌 이 값 기반으로 교체. 잠재변수 V1·V2·V3 제거.
3. `_PCMFileWriter.write_sample_buffer`(`system_audio.py:387-396`)를 **PTS 정렬 기록**으로 전환: `expected_index = round((pts − pts0)·rate)`와 실제 write 위치가 어긋나면 무음 패딩/트림. drift와 탭 드롭이 동시에 제거되고, 어긋난 양이 로그로 관측된다.
4. `_audio_input_args`의 ±50ms 사각지대 제거(`recorder.py:309-314`) — 이제 offset이 신뢰할 수 있으므로 버릴 이유가 없다. `_trim_wav`는 `-c copy` 대신 `-c:a pcm_s16le` 재인코딩으로 샘플 정확 트림.
5. `app.py:820-851`의 프로브 유래 `mic_latency_correction_seconds` **자동 config 반영을 중단**. 마이크 지연이 필요하면 프로브가 아니라 CoreAudio의 `kAudioDevicePropertyLatency` / `kAudioStreamPropertyLatency` / safety offset을 읽는다(관측 가능한 양이다).

**2순위 (검증 레이어): 대안 2 — 자기검증과 drift 보정**
참조 오디오 트랙을 켜고(= 대안 1의 플래그) 최종 mux에서 버리며, sys.wav와 교차상관해 lag(t)를 적합한다. 이것으로 (a) 1순위가 옳은지 **외부 계측기 없이 산출물만으로 증명**하고, (b) 잔여 drift를 보정하고, (c) 참조 트랙에 popping이 재현되는지 자동 판정한다.

**3순위 (단순화): 대안 1**
대안 2가 "참조 트랙에 popping 없음 + Δ ≈ 0"을 N회 연속 보고하면, sys.wav와 offset 계산 전체를 삭제하고 SCRecordingOutput에 영상+시스템오디오를 맡긴다. 마이크는 포럼 보고(mp4 손상) 때문에 당분간 별도 트랙 유지.

**예비: 대안 3**
실시간 미리보기나 저지연 요구가 생길 때만, 그리고 Python이 아니라 Swift 헬퍼로.

**자기검증 가능성이 왜 중요한가.** 이 앱은 메뉴바 백그라운드 도구다. 사용자는 진단 모드를 켜지 않고, 싱크 회귀는 누군가 영상을 실제로 볼 때까지 보이지 않는다. 편향이 미지인 계측기로 맞춘 캘리브레이션 상수에 정확성이 의존하는 설계는 새 하드웨어(Bluetooth 헤드셋, 120Hz 외장 디스플레이, 다른 Mac)에서 **조용히** 무너진다. 구성적으로 옳은 설계(1·3·5)는 런마다 검증이 필요 없고, 측정하는 설계(2)는 스스로 검증한다. 대안 4만은 어느 쪽도 아니다 — 유일한 검증 수단이 검증 대상과 얽혀 있기 때문이다.

#### 6. 다른 관점에서 나올 반론과 그에 대한 예비 답변

**"대안 1 한 줄이면 되는데 왜 대안 5부터?"** — popping의 실체가 여전히 미확인이고, 실패 모드가 "최종 산출물 손상 + 자동 검출 불가"다. 대안 5는 popping 위험을 전혀 만들지 않으면서 같은 정확도에 도달한다. 대안 1은 대안 2로 안전하게 검증한 뒤 채택하는 것이 순서다. 반대로 대안 1이 완전히 검증되면 대안 5는 삭제해도 되는 코드이므로 매몰비용도 작다.

**"프로브 개선(음향 왕복 캘리브레이션)으로 편향을 뺄 수 있다"** — 미지수 3개(τ_tap, δ_disp, q_frame)를 채널 1개로 분리할 수 없다는 것이 §3의 결론이다. 채널을 늘리려면 외부 카메라(단일 시계 관측자)나 가상 루프백 + 프레임 PTS가 필요하고, 후자를 구현하면 프로브 자체가 불필요해진다.

**"실측 편차가 이미 ±17/−32ms까지 좋아졌으니 대안 4로 충분하다"** — 그 두 값은 프로브의 잡음 바닥(±30ms 수준, 33ms 프레임 양자화 + 컴포지팅 지연)과 구별되지 않으므로 "좋아졌다"는 증거가 아니다. 같은 표본의 −409ms가 모델 붕괴를 증언한다. 더 결정적으로, 세 표본 전부 수초짜리 프로브 녹화라 **1시간 회의에서 36~360ms/시간으로 누적되는 drift 항이 측정 자체에 나타나지 않았다** — 대안 4는 이 항에 대해 아무 대책이 없다.

**"성능·안정성 관점에서 프레임 콜백은 위험하다"** — 대안 3에는 타당한 지적이며 그래서 3순위 이하로 뒀다. 대안 5는 **첫 프레임 하나만 받고 stream output을 즉시 제거**하므로 상시 프레임 콜백이 없다. 비용은 사실상 0이다.

**"pause/resume·다중 세그먼트가 더 큰 오차원이다"** — 동의하며 이는 대안 5를 더 강하게 지지한다. 세그먼트마다 `active_segment_started_at`을 다시 찍는 현재 방식(`recorder.py:796-798`)은 V1을 세그먼트 수만큼 곱하고, `setpts=PTS-STARTPTS`(`:1096`)가 영상 타임라인을 재기점화하는 동안 오디오는 각자 offset으로 trim되어 경계마다 새 잠재변수가 생긴다. 대안 5는 세그먼트별로 같은 방식(첫 프레임 PTS)을 재사용하므로 오차가 누적되지 않고, 대안 2는 경계 정렬을 사후에 측정으로 확인할 수 있다.

**출처**
- [Capturing screen content in macOS — Apple Developer Documentation](https://developer.apple.com/documentation/ScreenCaptureKit/capturing-screen-content-in-macos)
- [addStreamOutput(_:type:sampleHandlerQueue:) — Apple Developer Documentation](https://developer.apple.com/documentation/screencapturekit/scstream/3928168-addstreamoutput)
- [ScreenCaptureKit recording output is corrupted when captureMicrophone is true — Apple Developer Forums](https://developer.apple.com/forums/thread/805892)
- [deviceCurrentTime — Apple Developer Documentation](https://developer.apple.com/documentation/avfaudio/avaudioplayer/devicecurrenttime)
- [Recording to disk using ScreenCaptureKit — Nonstrict](https://nonstrict.eu/blog/2023/recording-to-disk-with-screencapturekit/)
- [ScreenCapture + CMSampleBuffer logic issue — Apple Developer Forums](https://developer.apple.com/forums/thread/794659)
- [SwiftUI: Screen Capturing on macOS — Level Up Coding](https://levelup.gitconnected.com/swiftui-screen-capturing-streaming-sharing-recording-on-macos-1550e0abd64e)


---

# 2라운드 — 상호 반박

각 에이전트에게 다른 두 관점의 주장을 요약해 전달하고, 수용/거부를 명시하며 입장을 수정하거나 방어하도록 요구했다. 강제한 쟁점: (1) 316ms의 원인, (2) 앵커 정밀화만으로 충분한가, (3) `-itsoffset`+`amix` no-op 여부와 처방, (4) `addRecordingOutput` 호출 순서, (5) `capture_audio=True` 채택 시점, (6) 결정적 버그들의 우선순위.


## [2R] 관점 A — 시계 도메인 이론

### 관점 A 2라운드: 반박과 수정

#### 1. B의 핵심 반론에 대한 답 — 부분 수용, 그러나 B의 모델도 부호를 설명하지 못한다

**수용**: 내 1라운드의 앵커 오차 모델은 **스칼라 편향(bias) 모델**이고, 사실 6(+17 / −32 / −409ms)의 ±수백 ms 분산을 만들 수 없다. B가 맞다. 앵커 오차만으로는 최대 −90ms(`recordingDidStart` 잔차) 규모가 상한이며, 그것도 부호가 한쪽으로 치우친다. 내 H1을 "잔차 원인"에서 "**분산의 주 원인**"으로 승격했던 것은 과대평가였고 철회한다.

**그러나 B의 모델도 단독으로는 틀렸다**: B의 버퍼 유실 모델은 **단측(항상 오디오가 앞으로 당겨짐)**이다. B 자신이 그렇게 명시했다. 그런데 사실 6에는 **+17ms(오디오가 늦음)**이 있다. 단측 생성기는 양쪽 부호를 만들지 못한다. 따라서 참된 생성 모형은 단일 원인이 아니라 **합**이다:

```
final_delta = bias(L, ±50ms 데드존, AAC 프라이밍 21~44ms, 프레임 양자화 ±17ms)
            − Σ(유실 버퍼 × 21ms)          [B, 단측·무작위]
            − Σ(세그먼트 tail 불일치)       [D9, 단측·결정적]
            − offset₀ (다중 세그먼트일 때)   [D7, 단측·결정적]
```

**여기서 내가 B에게 반격하는 지점**: −409ms를 가장 잘 설명하는 것은 B의 버퍼 유실(19개 버퍼 연속 유실을 요구)이 아니라 **B 자신이 찾아낸 D7 이중 trim**이다. `first_sys_offset = sys_segments[0][1]`(recorder.py:936)이 반환하는 값은 정확히 **0.1~1.0초 규모**(사실 1·3이 말하는 앵커 편차 규모)이고, 다중 세그먼트 실행에서만 발동하며, 항상 오디오를 앞으로 당긴다. −409ms는 "그 실행의 offset₀"에 지나지 않을 가능성이 매우 높다. 나는 **D7을 −409ms의 1순위 원인으로 승격**할 것을 제안한다. 이는 B의 우선순위(WAV 타임라인 압축 1순위)에 대한 수정 제안이다 — D7은 결정적이고, 규모가 정확히 맞고, 한 줄 수정이며, 검증이 즉시 가능하다(`self._segments` 길이 로그와 final_delta의 상관).

**"L vs 버퍼 유실은 진단 지표상 구분 불가"** — **인정한다.** 현재 계측 체계(단일 클릭 onset + 단일 플래시)로는 두 원인이 동일한 관측치를 만든다. 단 이것은 영구적 한계가 아니다. 두 가지로 즉시 분리된다: (a) 버퍼별 PTS 연속성 로깅 — 유실이 있으면 `expected_pts` 불연속으로 **직접** 보인다(B/C의 처방과 동일); (b) 출력 디바이스를 내장 스피커 ↔ Bluetooth로 바꿔 재측정 — L은 변하고 유실률은 변하지 않는다. 즉 **관측 가능성을 먼저 만들면 구분 가능**해지므로, 이 논쟁은 "누가 맞나"가 아니라 "PTS 로깅을 먼저 넣자"로 수렴한다.

#### 2. C의 `addRecordingOutput` 순서 발견 평가 — 사실 확인됨, 그러나 H1을 대체하지 않는다

문서 확인 결과 C가 맞다. Apple의 `addRecordingOutput` 바인딩 문서에 명문 규정이 있다: *"To guarantee the first sample captured in the stream to be written into the recording file, client need to add recordingOutput before startCapture."* ([dotnet/macios ScreenCaptureKit 바인딩 문서](https://github.com/dotnet/macios/wiki/ScreenCaptureKit-macOS-xcode16.0-b1))

그리고 `ContinuousCaptureController.start()`는 `self._driver.start_stream()`(continuous_screen_recorder.py:92) → `self._start_new_segment()`(:95) 순서다. 즉 **문서가 금지한 순서를 정확히 따르고 있다.** 사실 1(0.14~1.09초 편차)은 버그의 증상이 아니라 이 순서의 **정의상 귀결**이다 — `startCapture` 완료 후 recording output을 붙일 때까지의 임의 지연 동안 프레임이 버려지고, 그 지연이 곧 편차다. C의 진단을 전면 수용한다.

**H1의 순위는 바뀌는가**: 부분적으로. 순서를 고치면 `stream_capture_started_at`이 처음으로 **의미 있는** 앵커가 되고(pts 0 ≈ startCapture 직후 첫 프레임), `_screen_video_anchor`(recorder.py:341-362)의 후보 선택 로직 자체가 무의미해진다. 그러나 **H1은 유지된다**: 순서를 고쳐도 `_on_stream_started`와 `_on_recording_started`는 여전히 `time.time()`(continuous_screen_recorder.py:424, :430)이고, 디스패치 큐 도착 지연은 그대로다. 정확도는 "0.14~1.09초 편차의 랜덤 항이 사라지고 −40~−90ms 잔차만 남는" 상태가 된다.

**그래서 `recordingDidStart`만으로 충분한가**: **대부분의 경우 충분하다.** 정직하게 말하면, 잔차 ~50ms는 현재 파이프라인에 이미 박혀 있는 **±50ms 데드존**(recorder.py:310-313)과 **AAC 프라이밍 21~44ms**보다 크지 않다. 따라서 나는 1라운드에서 "1순위"로 제시한 **프레임 PTS 관측을 4~5순위로 강등**한다. 이것은 내 입장의 실질적 수정이다. PTS 관측은 (a) 데드존과 프라이밍을 먼저 제거한 뒤, (b) drift 보정을 넣은 뒤에야 측정 가능한 이득을 낸다. 다만 관측 자체는 **감시 계측기로서** 저비용·고가치이므로(C의 (2)와 동일) 조기 도입은 지지한다 — 앵커 계산에 쓰기 위해서가 아니라 H1의 크기를 실측으로 고정하기 위해서다.

#### 3. H2(출력 지연 L = 316ms) 재평가 — **범위를 축소해 유지**

C의 지적은 정확하다. 실제 회의 콘텐츠에서는 **sys 경로의 L이 대체로 상쇄된다**: 화면 캡처는 윈도서버 합성 시각을, 오디오 탭은 믹서 출력 시각을 찍으므로 둘 다 "presentation 이전" 지점이며, 앱이 스스로 A/V 동기를 맞춰 렌더링했다면 그 동기가 보존된다. 따라서 **L은 프로덕션 sys 앵커 오차의 원인이 아니다.** 이 부분은 철회한다.

**유지하는 부분**: H2는 원래 "**계측기의 기준 편향**"에 대한 주장이었고, 그 형태로는 오히려 강화된다. 프로브의 기준은 `deviceCurrentTime`(sync_diagnostics.py:267)이며, Apple 문서는 이것이 출력 디바이스 클럭이고 *"If the audio output device has no connected audio players that are either playing or paused, device time reverts to 0"*라고 명시한다 ([AVAudioPlayer.deviceCurrentTime](https://developer.apple.com/documentation/avfaudio/avaudioplayer/1387462-devicecurrenttime?language=objc)) — wall clock과 위상이 고정돼 있지 않다. 그리고 그 시각은 **presentation 시각**이고 탭은 pre-DAC이므로, 프로브의 클릭 기준과 탭 타임라인 사이에 L만큼의 계통 편향이 남는다. 즉 **316ms는 앵커 오차가 아니라 계측기 오차의 후보**다.

동시에 B가 옳게 지적했듯 **316ms는 15개 버퍼 유실로도 동일하게 설명된다.** 그러므로 H2의 최종 지위는: *"316ms에 대해 최소 두 개의 구분 불가능한 설명이 있으므로, **316ms를 근거로 어떤 보정값도 적용해서는 안 된다**"*. 이는 C의 결론(구조적 미식별)과 정확히 같은 지점이다. 그리고 C의 파생 발견 — `recommend_sync_adjustments`(sync_diagnostics.py:729-732)가 이 음향 왕복 편향을 `mic_latency_correction_seconds`로 산출해 `app.py:820-851`이 config.yaml에 **영구 기록**한다는 것 — 은 이 논쟁에서 나온 **가장 위험한 단일 발견**이다. 계측기의 미식별 편향이 프로덕션 상수로 승격되는 경로다. 전면 수용하며, 즉시 차단 대상으로 격상한다.

#### 4. `-itsoffset` + amix no-op 확정 및 정확한 처방

**확정한다.** 근거 두 겹:
- ffmpeg 문서: *"-itsoffset offset: ... The offset is added to the timestamps of the input files. Specifying a positive offset means that the corresponding streams are delayed."* → 디먹서 레벨 PTS 시프트일 뿐이다.
- `libavfilter/af_amix.c` 소스: 출력 PTS는 **입력 0의 frame_list에서만** 취한다(`s->next_pts = frame_list_next_pts(s->frame_list)`, 그리고 `if (i == 0) frame_list_add_frame(...)`). 나머지 입력은 `av_audio_fifo_write`로 그대로 쌓인다. **입력별 PTS 정렬도, 헤드 무음 삽입도 없다.**

따라서 `recorder.py:1618`의 `[1:a:0][2:a:0]amix`에서 **mic의 `-itsoffset`은 완전한 no-op**이고, sys의 `-itsoffset`은 출력 PTS를 밀어 mov `elst`로 나가 플레이어별로 해석이 갈린다. 추가 확정 사항: 문서는 `-ss` + **stream copy**일 때 *"this extra segment between the seek point and position ... will be preserved"*라고 명시하므로, `_trim_wav`의 `-ss ... -c copy`(recorder.py:1013)는 정확 trim이 아니라 seek point 기준 trim이다(PCM은 `ff_pcm_read_seek`가 block_align 정렬 바이트 위치를 계산하므로 실질적으로 샘플 정확이지만, 이 정확성은 **구현 우연**이며 문서 보장이 아니다).

**정확한 처방** — `-ss`/`-itsoffset`을 싱크 목적에서 완전히 제거하고 전부 필터그래프 안에서 처리한다. 부호별로:

```
offset > 0 (오디오가 영상보다 먼저 시작 → 앞을 버림):
    [N:a]atrim=start_sample=<round(offset*48000)>,asetpts=N/SR/TB[aN]
offset < 0 (오디오가 늦게 시작 → 앞에 무음 삽입):
    [N:a]adelay=<round(-offset*48000)>S:all=1[aN]
```

`adelay`는 지연값에 `S` 접미사를 주면 **샘플 단위**로 해석하므로 ms 반올림 없이 샘플 정확하다. 전체 필터그래프:

```
-i video.mp4 -i sys.wav -i mic.wav
-filter_complex
 "[1:a]<sys 보정>[a1];[2:a]<mic 보정>[a2];
  [a1][a2]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[am];
  [am]alimiter=limit=0.97[aout]"
-map 0:v:0 -map [aout] -c:v copy -c:a aac -b:a 192k -ar 48000
```

`normalize=0` + `alimiter`로 바꾸는 이유는 싱크와 무관하지만, `normalize=1`은 활성 입력 수가 바뀔 때 이득이 점프하므로 dropout 구간에서 레벨 계단이 생긴다. **`apad`는 오답**이다 — 꼬리에 무음을 붙이는 필터이므로 헤드 지연에 쓸 수 없다.

#### 5. 최종 수렴안

**가장 근본적인 단 하나의 수정**: **오디오를 "시간축 없는 바이트 스트림"으로 취급하는 것을 멈춘다.** 구체적으로 `_PCMFileWriter`가 각 버퍼의 PTS를 기대치와 비교해 부족분은 무음으로 메우고 초과분은 잘라내며(system_audio.py:387-396), 그 결과 WAV의 샘플 인덱스 n이 항상 `first_pts + n/48000`(host clock)을 의미하도록 불변식을 세우는 것이다. 이 하나가 B의 1순위(유실 누적), 내 H3(디바이스 클럭 drift), 그리고 앵커 계산의 좌표계 문제(PTS−PTS 뺄셈이 비로소 정당해짐)를 **동시에** 해결하며, 세 관점이 모두 독립적으로 이 수정을 지목했다.

**실행 순위** (수정 비용 대비 기대 오차 감소 순):

1. **D7 이중 trim 제거** — `first_sys_offset`/`first_mic_offset`을 다중 세그먼트 경로에서 0.0으로 반환(recorder.py:936, :948). 한 줄, −409ms 규모.
2. **D9 pause 순서 역전** — 영상 세그먼트를 먼저 닫고 그 다음 오디오를 멈춘다(recorder.py:730-751). 현재 `_stop_mic`이 최대 10초 대기하므로(recorder.py:507) tail 불일치가 초 단위까지 커질 수 있다. 단측·결정적·누적.
3. **`-itsoffset`/`-ss` → `atrim`/`adelay=…S` 교체 + ±50ms 데드존 제거**(§4).
4. **`_PCMFileWriter` PTS 정렬 기록 + 콜백 예외를 `logger.error`로 승격**(system_audio.py:483-485의 침묵 삼킴 제거).
5. **`addRecordingOutput`을 `startCapture` 이전으로**(C 발견, 문서 근거 있음).
6. **프로브 유래 `mic_latency_correction_seconds`의 자동 config 반영 중단**(sync_diagnostics.py:729-732, app.py:820-851) — 미식별 편향의 프로덕션 승격 차단.
7. **첫 프레임 PTS 관측 전용 stream output 추가** — 앵커 교체가 아니라 H1 잔차의 상시 계측기로.
8. 장시간 drift 보정(실효 샘플레이트 산출 → `aresample`).

**수용**: B의 D6·D7·D9·D16 전부; B의 "스칼라 앵커 모델은 분산을 설명 못 한다"는 반론; B의 "L과 버퍼 유실은 현재 지표로 구분 불가"; C의 `addRecordingOutput` 순서 발견; C의 프로브 구조적 미식별 판정; C의 mic 보정값 config 승격 위험; C의 `fps=120` 무의미 지적; C의 `capture_audio=True` 즉시 채택 반대(포럼 보고 근거).

**거부**: (a) B의 D8 **효과** 서술 — `_concat_videos_normalized`/`_concat_segments` screen 분기가 죽은 코드라는 **사실**은 맞으나(recorder.py:978이 audio 모드에서만 호출), "영상은 pause 구간을 포함한 채 합쳐진다"는 **틀렸다**. pause 시 `removeRecordingOutput`으로 세그먼트 파일이 닫히고 pause 중 프레임은 아예 기록되지 않으며, `finalize_mp4_segments`(continuous_screen_recorder.py:174-216)가 `-c copy` concat으로 합친다. 따라서 영상 타임라인에서 pause는 이미 제거돼 있다. 죽은 코드는 정리 대상이지 싱크 원인이 아니다. (b) B의 "WAV 타임라인 압축이 1순위" — 원인으로는 유효하나 **−409ms의 설명으로는 D7에 밀린다.** (c) 내 1라운드의 "프레임 PTS 관측이 1순위" — 스스로 강등한다.


## [2R] 관점 B — 구현 감사

### 관점 B 2라운드: 로그 증거로 재정렬

#### 1. H1(WAV 타임라인 압축) — 순위를 3위로 내리고, 그 자리에 로그가 가리키는 다른 것을 올립니다

**증거를 확인했습니다. 버퍼 유실의 직접 증거는 없습니다.** `~/Library/Logs/auto-meeting-note-v2_audio.log` 233줄, 12개 세션 전체에 `오디오 버퍼 처리 오류` / `PTS 변환 실패` / 포맷 실패가 **0건**입니다. 따라서 제 **D2(예외 삼킴)·D3(앵커가 미기록 샘플을 가리킴)은 이 실측 세트에서 발현하지 않았고**, "사실 4·5를 D1이 설명한다"는 제 1라운드 주장은 근거가 약합니다. 철회합니다. D1의 gap/회계 부재는 여전히 **잠재** 결함이고, C의 ppm 드리프트 논거(36~360ms/시간)와 결합해 **장시간 녹화**에서 지배적이 되지만, 수 초짜리 프로브의 분산을 설명하는 1순위는 아닙니다. A의 지적을 수용합니다.

**그러나 같은 로그에서 훨씬 강한 것이 나왔고, 이것이 논쟁의 프레임을 바꿉니다.**

- PTS 기반 앵커는 **매우 안정적**입니다: 계측된 4개 세션 전부 콜백 도착 대비 **-0.0718 / -0.0796 / -0.0677 / -0.0799s** (log:178, 195, 212, 229). 산포 12ms. 즉 "backlog burst 로 콜백이 0.08~1.5초 늦는다"는 사실 3은 **PTS 앵커에는 전이되지 않습니다** — A가 맞습니다.
- 반면 `startCapture 완료 → 첫 오디오 콜백` 간격은 세션마다 **4ms ~ 2.19초**로 극단적으로 갈립니다: log:124→128 은 `21:25:39.845 → 21:25:42.038` = **2.193초**, log:156→160 은 `21:32:02.856 → 21:32:04.984` = **2.128초**. 그런데 PTS는 첫 샘플이 콜백보다 겨우 70~80ms 전에 캡처됐다고 말합니다.
- 두 사실을 합치면 결론은 하나입니다: **탭이 2.1초 동안 아무 샘플도 만들지 않았고, WAV의 sample 0 은 영상 t=0 보다 최대 2.1초 늦습니다.** 이것은 앵커 오차가 아니라 **실재하는 오디오 head 결손**입니다.

이 경우 `offset = screen_anchor − audio_anchor` 는 **큰 음수**가 되고, 곧바로 두 개의 무보정 경로로 떨어집니다: `_audio_input_args` 의 `-itsoffset`(→ amix 안에서 no-op, A·B 합의) 과 `_trim_wav` 의 `offset <= 0.05 → shutil.copy2`(recorder.py:1009-1012, 무음 프리펜드 없음). **즉 오디오가 최대 2초 앞당겨진 채 그대로 muxing 됩니다.** 여기에 `_AUDIO_ANCHOR_WAIT_SECONDS = 3.0`(recorder.py:31)은 2.19초 케이스를 겨우 통과했을 뿐이고, 3초를 넘으면 폴백 사다리가 `started_at`(= 진짜 오디오 t0보다 2.1초 **이른** 값)을 집어 `-ss 2.1` 로 **실제 음성 2초를 잘라버립니다**(D12). 사실 5의 −409ms 는 이 축의 완화된 값으로 보는 것이 가장 자연스럽습니다.

**따라서 제 순위는 이렇게 바뀝니다: D6(음수 offset 무보정) 1위, C의 addRecordingOutput 순서 2위, D7/D8/D9(세그먼트 확정 버그) 공동 2위, D1 3위.**

A의 H2(스피커 출력 지연 L, 316ms ≈ Bluetooth 전형값)는 **프로브 계측 편향의 설명으로는 수용**합니다 — PTS 앵커의 12ms 산포는 앵커 자체가 흔들리지 않는다는 뜻이므로, 316ms는 앵커가 아니라 기준선이 움직였다고 봐야 합니다. 다만 이것은 **계측기 결함**이므로 녹화 경로에서 고칠 것이 없고, C의 "구조적 미식별" 판정과 합쳐 **프로브 기반 자동 보정(sync_diagnostics.py:729-732 → app.py:843)은 즉시 중단**해야 한다는 결론으로 이어집니다. 이 부분은 C에 전적으로 동의하며, 제 D15보다 C의 진단이 정확합니다.

#### 2. C의 addRecordingOutput 순서 발견 — 확인했고, 수용합니다

`continuous_screen_recorder.py:92-95`: `self._driver.start_stream()`(내부에서 `startCaptureWithCompletionHandler_` 완료를 `ready.wait` 로 대기, :327-336) → 그 다음 `_start_new_segment()` → `start_segment()` → `addRecordingOutput_error_`(:365). **C의 지적대로 recording output 이 startCapture 이후에 추가됩니다.** 이것이 사실 1(0.14~1.09초)의 필연적 원인이라는 판단에 동의합니다.

편입 방식: 이것은 "어느 앵커가 맞나"의 문제가 아니라 **|offset| 을 크게 만드는 발생원**입니다. offset이 작으면 D6·D11(50ms 데드존)·AAC 프라이밍 같은 잔차가 상쇄 범위 안에 들어오지만, offset이 0.5~2초면 같은 결함이 초 단위 사고가 됩니다. 순서를 뒤집어 `addRecordingOutput` → `startCapture` 로 만들면 영상 t0 ≈ 스트림 t0 ≈ 오디오 t0 가 되어 **offset 자체가 ~0 으로 수축**하고, 제가 1위로 올린 D6의 발현 확률도 같이 떨어집니다. 제 목록의 D9와 같은 층(라이프사이클 순서 결함)에 놓고, **수정 난이도 대비 효과가 가장 큰 단일 변경**으로 평가합니다.

#### 3. 확정적 버그 재확인 + 수정 diff

**D7 이중 trim — 확정.** `_concat_screen_sys_segments` 는 `sys_segments` 전부(i=0 포함)를 `_trim_wav(…, sys_offset, …)` 로 이미 자르는데(recorder.py:1195-1199), `stop()` 은 `first_sys_offset = sys_segments[0][1]`(:935)과 `first_mic_offset = self._screen_mic_segments[0][1]`(:948)을 반환해 `merge_audio_into_mp4` 가 `-ss` 로 **한 번 더** 자릅니다. 같은 파일 `_concat_segments` 는 `0.0, 0.0` 을 반환합니다(:1309-1310) — 자기모순 확정.

```diff
@@ recorder.py:933-935
-                            final_sys = self._concat_screen_sys_segments(sys_segments)
-                            first_sys_offset = sys_segments[0][1]
+                            final_sys = self._concat_screen_sys_segments(sys_segments)
+                            # _concat_screen_sys_segments 가 세그먼트별 offset 을 이미 trim 함
+                            first_sys_offset = 0.0
@@ recorder.py:948
-                    first_mic_offset = self._screen_mic_segments[0][1] if self._screen_mic_segments else 0.0
+                    first_mic_offset = (
+                        self._screen_mic_segments[0][1] if len(self._screen_mic_segments) == 1 else 0.0
+                    )
```
(sys 쪽도 `len==1` 분기는 :930-931에서 이미 분리돼 있으므로 위 diff로 정확히 맞습니다.)

**D8 죽은 코드 — 확정.** `_concat_videos_normalized` 의 유일한 호출자는 `_concat_segments`(:1284)이고, `_concat_segments` 의 유일한 호출자는 `stop()` 의 **audio 분기**(:978)입니다. screen 분기는 `self._screen_recorder.stop()`(:921) → `finalize_mp4_segments` → `-c copy`(continuous_screen_recorder.py:196-208)로 갑니다. 즉 `_concat_segments` 의 screen 분기(:1262-1311)와 `_concat_videos_normalized` 는 **screen 경로에서 도달 불가**. 수정: `ContinuousScreenRecorder._finalize_segments` 가 다중 세그먼트일 때 PTS 정규화 concat 을 쓰도록 바꾸고(`setpts=PTS-STARTPTS` + 재인코딩), 동시에 오디오 세그먼트 길이를 영상 세그먼트 길이에 `apad`/절단으로 맞춘 뒤 concat.

**D9 pause 순서 — 확정.** `recorder.py:732-751`: sys stop → `_stop_mic()`(최대 10초 wait, :508) → `self._screen_recorder.pause()`. 영상만 그만큼 길어집니다.
```diff
@@ recorder.py:730-751 (pause, screen 분기)
+                self._screen_recorder.pause()   # 영상 먼저 끊어 tail 정렬
                 if self._sys_audio is not None:
                     ...
                 self._stop_mic()
-                self._screen_recorder.pause()
```
(resume 은 현재 순서 유지 — 오디오 head 초과분은 offset trim 으로 흡수되므로 안전.)

**D16 무라벨 amix — 확정, 단 현재는 잠복.** `recorder.py:1477` `f"{self._amix_filter()}[aout]"` 는 입력 라벨이 없어 "미사용 오디오 스트림 앞 2개"를 자동 선택합니다. 지금은 `mov_path` 가 무음(capture_audio=False)이라 우연히 맞지만, `capture_audio=True` 나 외부 mov 가 들어오면 mic 대신 mov 오디오가 섞입니다. 수정은 `merge_audio_into_mp4` 와 동일하게 `[1:a:0][2:a:0]` 명시. (덧붙여 `mic_echo_cancel` 은 app.py:1521-1530 이 항상 `merge_audio_into_mp4` 를 타므로 **실행되지 않는 기능**입니다.)

#### 4. `-itsoffset` 대체 — 완전한 필터그래프

sys/mic 각각 `trim_x = max(0, off_x)`, `delay_x_ms = round(max(0, -off_x) * 1000)`, `rate` 는 해당 WAV 실제 sample rate.

```
-i VIDEO.mp4 -i sys.wav -i mic.wav -filter_complex
"[1:a]atrim=start_sample=<round(trim_sys*rate_sys)>,asetpts=N/SR/TB,
      adelay=<delay_sys_ms>:all=1,apad[a1];
 [2:a]atrim=start_sample=<round(trim_mic*rate_mic)>,asetpts=N/SR/TB,
      adelay=<delay_mic_ms>:all=1,apad[a2];
 [a1][a2]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mx];
 [mx]alimiter=limit=0.95:level=disabled[aout]"
-map 0:v:0 -map "[aout]" -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest
```
핵심: (a) `-ss`/`-itsoffset` 완전 제거 — 이동은 전부 필터 안 샘플 단위로, 데드존 없음(50ms 버림 제거); (b) 음수 offset 은 `adelay` 로 **앞을 실제 무음 패딩**; (c) `apad` + `duration=longest` + `-shortest` 로 길이는 영상이 결정 → dropout 레벨 점프 제거; (d) `normalize=0` + `alimiter` 로 진폭 반감(현재 `normalize=1`의 1/2 감쇠)을 없애고 클리핑만 제어. `_trim_wav` 도 같은 방식으로 `atrim`+`adelay` 로 바꿔 음수 offset을 처리해야 합니다(현재는 copy).

**갱신할 테스트**: `tests/test_capture_sync.py:244-247`(`["-ss","0.300","-i",…]`, `["-ss","0.120",…]` 단정) → `filter_complex` 문자열에서 `atrim=start_sample=14400`, `adelay=0`(양수 케이스) 를 단정하도록 재작성. 음수 케이스(`audio_offset=-0.30`)를 **신규 추가**해 `adelay=300` 이 나오는지 검증(현재 이 케이스를 검증하는 테스트가 없어서 D6가 살아남았습니다). :123-190 의 `mic_latency_correction` 테스트와 :206-208 의 앵커 로그 단정은 영향 없음.

#### 5. 최종 수렴안

**가장 근본적인 단 하나의 수정: 영상 t0 과 오디오 t0 을 같은 host clock PTS 로 얻고, 그 차이를 샘플 단위 `atrim`/`adelay` 로 적용한다** — A의 "PTS끼리 직접 비교"(수용)와 제 "적용 경로가 부호 비대칭"(D6)의 교집합입니다. 영상 PTS를 얻으려면 recording stream 에 `SCStreamOutputTypeScreen` output 을 하나 붙여 첫 프레임 PTS만 읽으면 되고(픽셀 미사용), 그러면 wall clock·`recordingDidStart`·`-40~-90ms` 편향·데드존이 한꺼번에 사라집니다.

우선순위 실행 계획:
1. **`addRecordingOutput` → `startCapture` 순서 교정**(C, 수용). offset 자체를 수축시켜 나머지 결함의 노출을 줄임. 1시간.
2. **`-ss`/`-itsoffset` → `atrim`/`adelay` 필터그래프 교체 + `_trim_wav` 동일 적용**(A·B·C 합의). 음수 offset을 처음으로 실제 처리. 반나절.
3. **D7 이중 trim(2줄), D16 라벨 명시(1줄), D9 pause 순서(3줄).** 1시간.
4. **프로브 기반 `mic_latency_correction` 자동 반영 중단**(C, 수용) — `app.py:843` 를 로그 전용으로 격하. 30분.
5. **영상 첫 프레임 PTS 앵커 도입**(A·B 수렴). 1~2일.
6. **`_PCMFileWriter` PTS 회계 + gap 패딩 + 드리프트 카운터**(B·C). 로그에 유실 증거는 없지만 **장시간 녹화용 안전장치이자 관측 수단**이므로 유지. 반나절.
7. **D8 세그먼트 concat 정규화 + 오디오/영상 세그먼트 길이 정합.** 1일.

**수용**: A의 PTS-대-wall clock 프레임, A의 L(출력 지연) 프로브 편향 설명, A의 AAC 프라이밍·데드존 중첩, C의 addRecordingOutput 순서, C의 프로브 미식별성, C의 자동 보정 중단, C의 단일 스트림 즉시 채택 반대(마이크 동시 사용 시 mp4 손상 보고 — 대안 A는 검증 후로 연기).
**거부**: "D1이 사실 4·5의 1순위"라는 제 1라운드 주장(로그 반증). "프로브로 잔차를 캘리브레이션하면 된다"는 접근(미식별). 그리고 **"앵커만 고치면 된다"는 A의 함의는 여전히 거부**합니다 — 로그가 보여준 2.1초 head 결손은 앵커가 **정확히 맞아도** 현재 적용 경로가 음수 offset을 버리기 때문에 그대로 사고가 되고, D7/D8/D9는 앵커와 완전히 무관하게 확정적입니다.


## [2R] 관점 C — 관측가능성·설계 대안

### 관점 C 2라운드: 반박과 수정

#### 1. A의 H2 기각 — 스피커 지연 L은 sys 경로에 들어올 물리적 경로가 없다

신호 경로를 단계로 끊으면 결론이 나온다.

```
AVAudioPlayer 렌더 → [HAL 출력 믹스] → DAC/BT 인코드·전파·디코드 → 스피커 진동 → 공기 → 마이크 → HAL 입력
                          ↑ 탭
```

- `deviceCurrentTime`은 문서상 "the time value, in seconds, of the audio output device's clock"이며 구현은 `CAHostTimeBase::GetCurrentTime()`이다. 즉 **host time 도메인의 현재 시각**이고, `playAtTime:`은 그 시계 기준 예약이다.
- SCStream 오디오 탭은 **출력 믹스 지점**에서 샘플을 취하고 그 버퍼에 같은 host clock 도메인의 PTS를 찍는다.
- 두 시각은 체인의 **같은 지점(믹스)** 을 참조한다. 따라서 둘의 차이는 IO 사이클 + safety offset 규모, 즉 **수 ms ~ 수십 ms**다.
- L(= DAC 이후의 모든 것: 아날로그, 스피커 DSP, BT 전송)은 **믹스 하류에서만** 발생한다. 하류 지연은 (a) 사람이 듣는 시각, (b) 마이크에 도달하는 시각에만 영향을 준다. **탭에 이미 찍힌 샘플의 위치를 되돌려 바꿀 경로가 없다.**

A의 반론 가능성 — "탭 PTS가 BT aggregate 지연을 반영한다면?" — 도 성립하지 않는다. 시스템이 BT 지연을 노출하는 창구는 `kAudioDevicePropertyLatency` / `outputLatency`이고, Apple 포럼에 반복 보고된 불만은 이 값이 **실제 BT 지연보다 훨씬 작게** 나온다는 것이다. 시스템은 L을 과소보고하며, 과소보고는 316ms를 만들지 못한다.

**결론: A의 H2는 물리적으로 성립 불가. 기각.** 다만 A가 가리킨 L 자체는 실재한다 — **위치가 틀렸을 뿐이다.** L은 마이크 경로에서만 미상쇄로 남고, 바로 그 값이 `recommend_sync_adjustments`(`sync_diagnostics.py:729-732`)를 통해 `mic_latency_correction_seconds`로 산출되어 `app.py:843`에서 `config.yaml`에 영구 기록된다. 즉 A의 H2와 내 1라운드 §3 지적은 **같은 물리량의 서로 다른 위치**를 말하고 있고, 이 항이 실제로 해를 끼치는 곳은 sys가 아니라 mic다. 이 부분은 A의 통찰을 수용하되 배치를 정정한다.

**판정 실험(재측정 지시 위반 없이 제시)**: 출력 디바이스를 BT ↔ 내장 스피커로 바꿔 프로브를 2회 돌린다. A가 맞다면 sys 편차가 150~300ms 변해야 한다. mic 편차만 변하고 sys 편차가 변하지 않으면 내가 맞다. 기존 진단 로그만으로 판정된다.

#### 2. B의 "구분 불가"는 내 미식별 논지를 강화한다 — 그리고 탈출은 1줄이다

B의 메커니즘을 방향까지 검산했다. 클릭 전에 N개 버퍼가 유실되면 WAV의 클릭 위치가 그만큼 **앞으로** 당겨지고 → `raw_system_delta` 감소 → `sys_anchor_error = applied − raw_delta` **증가** → 진단은 "PTS 앵커가 실측 대비 이르다"로 출력한다. 사실 4의 표현과 부호·방향이 정확히 일치한다. 15버퍼 × 1024프레임/48kHz ≈ 320ms로 크기도 맞다. **B의 메커니즘이 316ms의 최유력 설명이다.**

그리고 B가 맞다: 현재 계측으로 "버퍼 유실"과 "앵커 편향"은 **동일한 관측량 하나에만 영향**을 주므로 구분 불가다. 이것은 내 1라운드 논지("채널 1개, 미지수 다수 → 구조적 미식별")의 강한 확증이며, 미지수 목록에 **N_dropped(시변, 부하 의존)** 를 추가해야 한다. 미식별의 심각도가 올라갔다.

**미식별에서 벗어나는 최소 변경**: `write_sample_buffer`에서 버퍼마다 `expected = round((pts − pts0) · rate)`를 실제 기록 프레임 수와 비교해 불일치를 로그로 남기는 것. **한 곳의 계측 추가로 N_dropped가 관측량이 되고, 316ms 논쟁이 한 번의 세션 재분석으로 종결된다.** 내 5b(PTS 정렬 기록)는 그 위에 교정까지 얹는다.

단 5b만으로 부족한 두 가지를 인정한다:
- **B의 D3**: `first_sample_host_at`이 write **시도 전에** 설정되므로(`system_audio.py:472-481`), pts0 확정을 **write 성공 이후로** 옮겨야 한다.
- **5b는 정렬만 고친다.** 유실된 버퍼 자리에 무음을 채워 타임라인은 보존하지만 **오디오 내용은 여전히 사라진다.** 따라서 B의 성능 수정(순수 Python de-interleave `:330-337` → numpy/memoryview, system·mic **공유 락** `:480-481` 분리, `sampleHandlerQueue`에 `None` 대신 전용 dispatch queue `:589-594`, 예외 조용한 삼킴 `:483-485` 제거)은 **선택이 아니라 5b의 전제조건**이다. 이 구분을 1라운드에서 명시하지 않은 것을 정정한다.

#### 3. addRecordingOutput 순서 — 근거 등급 하향, 그러나 결론은 강화

재검증 결과 **내 1라운드 표현("문서가 보장한다")은 과장이었다.** Apple의 `addRecordingOutput(_:)` 레퍼런스 페이지를 JSON 엔드포인트까지 확인했으나 **Discussion 절이 존재하지 않는다.** "첫 샘플 보장"은 WWDC24 샘플의 호출 순서와 커뮤니티 문서(Level Up Coding 튜토리얼)에서 확인되는 **관례적 근거**이며, 규범적 API 보장으로 인용할 수 없다. 근거 등급을 "문서 보장" → "샘플 코드 관례 + 2차 출처"로 하향한다.

코드 확인은 재확인됐다: `ContinuousCaptureController.start()`는 `start_stream()`(= `startCaptureWithCompletionHandler_` 완료 대기, `continuous_screen_recorder.py:327-339`) → `_start_new_segment()` → `addRecordingOutput_error_`(`:365`) 순서다. **recording output은 확실히 startCapture 이후에 추가된다.**

**예측**: 순서를 고치면 세그먼트 0의 V1은 "recording output 세션 시작 ↔ startCapture" 정렬로 축소되어 0.14~1.09초 → **수십 ms 수준**으로 줄 것으로 본다. 부수 효과로 `stream_capture_started_at`(앵커 A)이 다시 유효해진다 — RELEASE_NOTES가 1.1.13에서 A로 갔다가 다시 B로 돌아온 진동(`RELEASE_NOTES.md:48` vs `recorder.py:341-362`)은 **이 순서 버그의 증상**이었다.

**그럼에도 대안 5는 여전히 필요하다.** 이유 두 가지:
1. **resume 세그먼트에는 적용 불가.** pause/resume은 라이브 스트림에 recording output을 중도 추가하므로(`recorder.py:795`) V1이 세그먼트마다 온전히 되살아난다. 순서 수정은 세그먼트 0만 구제한다.
2. **근거 등급이 관례 수준이므로 가정을 검증할 수단이 필요하다.** 첫 프레임 PTS를 관측하면 "pts0 == 첫 프레임" 가정이 참인지 산출물로 확인된다. 규범적 보장이 없다는 사실이 대안 5를 *더* 필수로 만든다.

#### 4. `capture_audio=True` 재평가 — 위험 하향, 단 관측성이 마이크로 이전한다

맞다. 내가 인용한 포럼 보고는 `captureMicrophone=true`에 한정되고, 이 프로젝트는 마이크를 별도 WAV로 유지할 수 있다(현재도 그렇다). **"영상+시스템오디오만 SCRecordingOutput, captureMicrophone off, 마이크 별도"** 조합에는 그 보고가 적용되지 않는다. 대안 1의 회귀 위험 평가를 **"높음" → "중간"** 으로 하향한다.

남는 위험:
- **popping(미검증).** 다만 `capture_audio=True` 경로에는 이미 `setSampleRate_(48000)`/`setChannelCount_(2)` 하드닝이 들어 있다(`continuous_screen_recorder.py:306-312`) — popping의 유력 원인(포맷 네고시에이션)에 대한 수정이 *비활성 경로에* 잠들어 있다.
- **인코더 파라미터 제어 불가.** STT는 16kHz mono를 쓰므로(`pipeline.py:155`) 실질 영향 없음.
- **결정적: 관측성 문제가 마이크로 이전한다.** sys가 파일 안으로 들어가면 sys PTS 관측점을 잃고, 마이크를 mp4 타임라인에 정렬할 기준이 사라진다.

**해법이 곧 수렴점이다.** 같은 SCStream에 (a) recording output(영상+sys, startCapture 이전 추가), (b) screen stream output(첫 프레임 PTS만 읽고 제거), (c) microphone stream output을 붙이면
`mic_offset = firstVideoPTS − firstMicPTS` — **wall clock 없이 host clock 내부에서 닫힌다.** 영상↔sys는 프레임워크 mux로 구성적 정확, 영상↔mic는 같은 시계의 PTS 차이. 여기에 sys를 audio stream output으로 **병행 수신**하면 대안 2(교차상관 검증)와 popping 폴백을 동시에 얻는다 — 단 §2의 콜백 성능 수정이 전제다.

#### 5. 최종 수렴안

**가장 근본적인 단 하나의 수정: 싱크 계산에서 wall clock을 완전히 축출한다.** 하나의 SCStream 안에서 영상+시스템오디오는 SCRecordingOutput이 mux해 뺄셈 자체를 없애고, 남는 마이크만 같은 host clock의 PTS 차이로 정렬한다.

**우선순위 (1라운드 대비 P0 승격을 명시)**

- **P0 — B의 D7·D8·D9·D5 + 콜백 견고성.** 앵커 정확도와 무관하게 pause/resume 사용 시 초 단위 오차를 만든다. **1라운드에서 내가 저평가했음을 인정하고 최상위로 올린다.** 특히 D7(`recorder.py:936,948`이 `first_sys_offset`을 반환해 `_trim_wav`로 이미 자른 것을 `-ss`로 재차 자름) 대 `_concat_segments`의 올바른 `0.0` 반환(`:1309-1310`)은 같은 파일 내 두 경로의 정면 모순이고, D8(`_concat_videos_normalized`가 screen `stop()` 경로에서 미호출)은 영상은 pause 구간 포함·오디오는 제거라는 **초 단위 불일치**를 확정적으로 만든다. 앵커를 완벽히 고쳐도 이것들이 남으면 결과물은 여전히 깨진다.
- **P1 — PTS 갭 계측(1곳) → 5b PTS 정렬 기록 → pts0 확정을 write 성공 이후로(D3).** 316ms 논쟁을 종결하고 drift·유실을 관측·교정 가능하게 만든다.
- **P2 — addRecordingOutput을 startCapture 이전으로 + 대안 5(첫 프레임 PTS 관측).** 앵커 미식별 해소. 순서 수정은 세그먼트 0만, 대안 5가 resume 세그먼트와 검증을 담당.
- **P3 — 대안 1(`capture_audio=True`, captureMicrophone off) + 대안 2(교차상관 검증·drift 적합).** 뺄셈 자체 제거 + 자기검증.
- **P4 — 대안 3은 Swift 헬퍼로만, 예비.** B와 합의.
- **즉시 무조건 — 프로브 유래 `mic_latency_correction_seconds`의 config 자동 반영 중단(`app.py:820-851`).** L을 타임스탬프 보정으로 승격시키는 경로이며, A의 H2 논의가 오히려 이 항의 실재성을 확증했다.

**수용**: A의 "오차원 혼합" 진단, AAC 프라이밍(21~44ms)·elst 항 — `_audio_input_args`의 ±50ms 데드존(`recorder.py:309-314`)과 합쳐 +17/−32ms를 설명한다는 해석은 설득력 있다. A의 "두 스트림 구조가 근본 원인이 아니다"는 **부분 수용**: PTS끼리 비교하면 두 스트림도 정확하고, 스트림 개수는 독립 변수다. 다만 하나로 합치는 것은 근본 원인이 아니라 **근본 해결책의 일부**다 — SCRecordingOutput에 sys를 맡기면 뺄셈이 소멸하고, 두 스트림은 두 개의 startup transient·두 개의 실패 모드를 뜻한다. B의 진단은 D7/D8/D9/D5·성능·D3 모두 수용.

**거부**: A의 H2(316ms = 스피커 출력 지연 L) — 신호 경로상 성립 불가, 대안 설명은 B의 버퍼 유실. **유보**: B의 "WAV 타임라인 압축이 잔차의 주원인"은 316ms급 **이상치와 실행 간 분산**을 설명하지만, 정상 케이스의 ±30~60ms **편향**은 여전히 영상 앵커의 V1·V2가 지배한다. 두 원인은 오차의 서로 다른 성분(분산 vs 편향)을 설명하며, 어느 하나만 고쳐도 허용치 80ms에 안정적으로 들어가지 못한다.

**출처**: [deviceCurrentTime — Apple Developer Documentation](https://developer.apple.com/documentation/avfaudio/avaudioplayer/devicecurrenttime) · [addRecordingOutput(_:) — Apple Developer Documentation](https://developer.apple.com/documentation/screencapturekit/scstream/addrecordingoutput(_:)) (Discussion 절 없음 — 확인) · [Capture HDR content with ScreenCaptureKit, WWDC24](https://developer.apple.com/videos/play/wwdc2024/10088/) · [SwiftUI: Screen Capturing on macOS — Level Up Coding](https://levelup.gitconnected.com/swiftui-screen-capturing-streaming-sharing-recording-on-macos-1550e0abd64e) · [How to get the output latency of Bluetooth audio — Apple Developer Forums](https://developer.apple.com/forums/thread/126277) · [ScreenCaptureKit recording output is corrupted when captureMicrophone is true](https://developer.apple.com/forums/thread/805892)


---

# 3라운드 — 조정자 판정과 최종 수렴

2라운드에서 세 관점이 정면 충돌한 두 지점을 조정자가 코드와 로그로 직접 판정한 뒤, 그 결과를 세 에이전트에게 동시에 제시하고 최종 수렴안을 요구했다.

## 조정자 판정 1 — D8(세그먼트 concat) 은 실재한다. A의 반박 기각

A는 "pause 시 `removeRecordingOutput` 으로 세그먼트가 닫히므로 영상 타임라인에서 pause 구간은 이미 제거돼 있다"고 반박했다. 그러나 저장소 자신의 docstring이 반대를 말한다.

```
recorder.py:1080-1088  _concat_videos_normalized()
"""비디오 세그먼트의 PTS를 0부터 다시 매기며 재인코딩으로 합친다.

SCRecordingOutput은 Mach time 기반 PTS를 기록해, -c copy 단순 결합 시
세그먼트 사이에 일시정지 시간만큼 frozen 구간이 생긴다.
setpts=PTS-STARTPTS 로 각 세그먼트 PTS를 0부터 재계산한 뒤 libx265 로 재인코딩한다.
"""
```

저자가 이 현상을 실제로 관측하고 우회 함수를 작성했으며, 그 함수는 screen 경로에서 도달 불가다(`stop()` screen 분기는 `self._screen_recorder.stop()` 만 호출 — `recorder.py:921`, 그리고 `finalize_mp4_segments` 는 `-c copy` — `continuous_screen_recorder.py:173-208`). 세그먼트 mp4 가 pts 0 으로 재기준화된다는 A의 가정은 이 기록이 부정한다.

조정자가 함께 확인한 사항:
- `_trim_wav` 는 `offset <= 0.05` 면 `shutil.copy2` — 음수 offset 은 무음 prepend 없이 버려진다 (`recorder.py:1007-1014`)
- `_audio_input_args` 의 ±50ms 데드존 (`recorder.py:309-313`)
- `merge_audio_into_mp4` 의 `has_sys and has_mic` 분기만 `amix` 를 타고(`recorder.py:1618`), `has_sys and not has_mic` 분기는 `-map 1:a:0` 직결(`recorder.py:1645-1652`) — **마이크 유무에 따라 `-itsoffset` 이 동작/무동작으로 갈린다**
- `amix ... normalize=1` (`recorder.py:305`) 은 진폭을 반감시킨다

## 조정자 판정 2 — B의 "오디오 head 결손 2.1초" 추론에 반례가 있다

B는 로그의 `startCapture 완료 → 첫 오디오 콜백` 간격 2.19초와 "PTS는 콜백보다 70~80ms 전"에서 **WAV sample 0 이 영상 t0 보다 최대 2.1초 늦다**고 추론했다. 반례:

- 그 세션(21:25:39) WAV 길이 9.88초, 내용은 클릭(위치 1.947s) 외 완전 무음(조정자가 파형 직접 확인)
- 첫 콜백이 21:25:42.038 이라면 WAV 끝 = 42.038 + 9.88 = **21:25:51.9**
- 그런데 병합 로그가 이미 **21:25:50.920** 에 찍혔다 → 물리적으로 불가능
- 파일 길이 역산 A0 ≈ 21:25:40.7~41.0 = 첫 콜백보다 **약 1.3초 이전** → **backlog 지연 전달 모드**

그리고 조정자가 `~/Library/Logs/auto-meeting-note-v2_audio.log` 에서 추출한 결정적 데이터 — PTS 가 기록된 4개 세션은 전부 정상이며, 병리적 2.1초 세션들에는 PTS 로그가 아예 없다:

| 세션 | `first_sample_at − startCapture` | `first_sample_host_at − startCapture` |
|---|---|---|
| 21:25:39 | **+2.193s** | (PTS 로깅 없음) |
| 21:32:02 | **+2.128s** | (PTS 로깅 없음) |
| 21:35:02 | +4.5ms | **−67.3ms** |
| 21:37:19 | +5.0ms | **−74.6ms** |
| 21:37:49 | +4.4ms | **−63.2ms** |
| 21:40:09 | +8.3ms | **−71.6ms** |

즉 B의 외삽은 근거가 없고, 두 병리 세션은 PTS 앵커를 쓰지 않은 실행이었다.


## [3R] 관점 A — 시계 도메인 이론

### 관점 A 3라운드 (최종)

#### 1. D8 판정 — **전면 수용**

수용한다. `recorder.py:1080-1085`의 docstring이 "SCRecordingOutput은 Mach time 기반 PTS를 기록해, `-c copy` 단순 결합 시 세그먼트 사이에 일시정지 시간만큼 frozen 구간이 생긴다"고 명시한다. 저자가 실제로 관측하고 우회 함수를 작성했으므로, 내 "세그먼트 mp4가 pts 0으로 재기준화된다"는 추론은 **경험적 반증에 밀린다.** 이론적 추측 대신 저장소 자신의 관측 기록을 채택하는 것이 옳다.

**순위 변경**: 이건 D7보다 심각하다. D7은 offset₀(0.1~1.0초)만큼의 **일회성 시프트**지만, D8은 영상에 pause 길이만큼 frozen 구간이 들어가고 오디오에서는 pause가 제거되므로, **pause 1회당 pause 지속시간(수 초~수십 초) 전체가 desync로 누적**된다. 즉 pause를 쓴 녹화는 근본적으로 복구 불가 상태였다. 이것을 실행 순위 **1번**으로 올린다. 처방은 두 갈래 중 하나: (a) `finalize_mp4_segments`를 `_concat_videos_normalized`(setpts=PTS-STARTPTS 재인코딩)로 교체, 또는 (b) 더 저렴하게 `-c copy` 유지 + 세그먼트별 `-fflags +genpts`/`setts` 없이는 안 되므로 사실상 (a)가 유일. 재인코딩 비용을 피하려면 SCRecordingOutput 대신 AVAssetWriter로 직접 PTS를 재기준화해 쓰는 2순위 안(C의 방향)이 필요해진다.

#### 2. 판정 2 — 로그로 부분 판별했다. 결론은 "**현재 데이터로 미결, 그러나 판별 불필요**"

PTS가 로깅된 세션 4개에서 `first_sample_host_at − capture_started_at`을 직접 계산했다(`~/Library/Logs/auto-meeting-note-v2_audio.log`):

| 세션 | host_at − startCapture | 콜백 − startCapture |
|---|---|---|
| 21:35:02 | **−67.3ms** | +4.5ms |
| 21:37:19 | **−74.6ms** | +5.0ms |
| 21:37:49 | **−63.2ms** | +4.4ms |
| 21:40:09 | **−71.6ms** | +8.3ms |

즉 **4개 모두 head 결손이 아니라 head 잉여(PTS가 startCapture보다 63~75ms 이르다)**, 산포 11ms. 그런데 이 4개는 전부 콜백 지연이 4~8ms인 **무backlog 세션**이다. backlog 세션(21:25:39의 +2.192s, 21:32:02의 +2.128s)에는 PTS 로깅이 없다. 따라서 조정자가 맞다 — **B의 외삽은 무효**이며, "탭이 backlog를 쏟아냈다" vs "탭이 2.1초간 침묵했다"는 **현재 데이터로 미결**이다.

**판별 절차**(이미 코드가 있으므로 추가 구현 0): backlog 세션이 재현될 때까지 `first_sample_host_at − capture_started_at`을 계속 로깅. 값이 −2.1초면 backlog 덤프(PTS 앵커가 정답), +2.1초면 진짜 head 결손(음수 offset 처리가 핵심). 판정 기준은 `|host_at − startCapture| > 0.15s`.

**그러나 이 판별은 수정에 필요하지 않다.** `offset = video_pts0_host − audio_first_pts_host`를 계산하고 부호에 따라 `atrim`(양수) / `adelay=…S`(음수)를 적용하는 구현은 **부호 불변(sign-agnostic)**이다. 두 실패 모드 어느 쪽이든 자동으로 올바르게 처리된다. 판별은 "고쳐졌는지 확인"용이지 "무엇을 고칠지 결정"용이 아니다. 이것이 3자 합의 지점이다.

#### 3. 최종 실행 계획 (합의안)

| # | 항목 | 위치 | 작업량 | 없으면 깨지는 것 |
|---|---|---|---|---|
| 1 | 영상 세그먼트 concat을 PTS 재기준화 경로로 교체 | `continuous_screen_recorder.py:174-216` → `recorder.py:1080` 사용 | 중 (재인코딩 비용 검토 포함) | pause 1회당 pause 길이 전체가 A/V desync로 남는다 |
| 2 | D7 이중 trim 제거 (`first_sys_offset`/`first_mic_offset` → 0.0) | `recorder.py:936, 948` | 소 (2줄) | 다중 세그먼트에서 offset₀(0.1~1.0s)만큼 오디오가 두 번 잘린다 |
| 3 | pause 순서 역전 (영상 세그먼트 먼저 닫고 오디오 정지) | `recorder.py:730-751`, `_stop_mic` 최대 10초 대기 `recorder.py:507` | 소 | 세그먼트 tail이 영상만 길어져 이후 전 세그먼트가 앞으로 밀린다 |
| 4 | `-ss`/`-itsoffset` 폐기 → 필터그래프 `atrim=start_sample=N,asetpts` / `adelay=NS:all=1` | `recorder.py:309-313, 1466-1467, 1608-1618, 1645-1652` | 중 | 음수 offset이 amix 경로에서 no-op이고, 마이크 유무에 따라 동작이 갈린다 |
| 5 | ±50ms 데드존 제거 + AAC 프라이밍(21~44ms) 명시 처리 | `recorder.py:309-313` | 소 | 최대 50ms + 프라이밍이 무조건 잔차로 남는다 |
| 6 | `_trim_wav`의 음수 offset 분기 추가(무음 prepend) | `recorder.py:1007-1014` | 소 | 세그먼트 경로에서 음수 offset이 조용히 버려진다 |
| 7 | `_PCMFileWriter` PTS 정렬 기록(gap→무음 삽입, 초과→절단) + 콜백 예외 `logger.error` 승격 | `system_audio.py:387-396, 483-485` | 중 | 버퍼 1개 유실마다 이후 전체가 21ms 영구 시프트, 관측 불가 |
| 8 | `addRecordingOutput`을 `startCapture` 이전으로 | `continuous_screen_recorder.py:92-95` | 소 | 첫 프레임 유실 + 사실 1의 0.14~1.09초 랜덤 편차 |
| 9 | 프로브 유래 `mic_latency_correction_seconds` 자동 config 반영 중단 | `sync_diagnostics.py:729-732`, `app.py:820-851` | 소 | 미식별 음향 왕복 편향이 프로덕션 상수로 승격된다 |
| 10 | 첫 프레임 PTS 관측 전용 stream output(감시용) + `host_at − startCapture` 상시 로깅 | `continuous_screen_recorder.py` | 중 | 판정 2가 영구 미결로 남고, 잔차 크기를 알 수 없다 |

#### 4. 단 하나의 근본 변경

> **모든 미디어 시각을 host clock PTS로만 표현하고, WAV의 샘플 인덱스가 항상 `first_pts + n/48000`을 의미하도록 불변식을 세운 뒤, 그 좌표계에서 계산한 offset을 샘플 단위 `atrim`/`adelay`로만 적용한다.**

이는 B의 문장("PTS끼리 비교 + 샘플 단위 atrim/adelay")과 **본질적으로 동일**하며, 내가 추가하는 것은 "WAV 불변식" 절 하나뿐이다(=B의 1순위인 타임라인 압축 방지를 같은 문장에 흡수). C의 문장("wall clock 완전 축출")과도 **목표는 동일**하지만 **수단은 다르다**: C는 단일 스트림 + SCRecordingOutput이 영상+sys를 함께 mux하는 구조를 제안하는데, C 자신이 인용한 `captureMicrophone=true` 시 mp4 손상 보고와 v1.1.13의 popping 회귀 이력을 감안하면 그 구조 변경은 **1~10번 수정 이후에 별도 검증을 거쳐야 하는 2단계**다. 즉 세 관점의 목표는 수렴했고, 남은 유일한 불일치는 "단일 스트림 mux로 갈 것인가"의 **시점**뿐이다 — 나는 이를 즉시 채택하지 말고 위 10개 항목 완료 후 재평가할 것을 권한다.


## [3R] 관점 B — 구현 감사

### 관점 B 3라운드 (최종)

#### 1. 판정 2 수용 — 실측 세션은 전부 "backlog 쏟아내기"

PTS가 기록된 4개 세션에서 `first_sample_host_at − started_at`(startCapture 완료)을 실제로 계산했습니다:

| 세션 | started_at | host_at | 차이 |
|---|---|---|---|
| 21:35:02 (log:173,178) | …902.294816 | …902.227493 | **−67.3ms** |
| 21:37:19 (log:190,195) | …039.427200 | …039.352627 | **−74.6ms** |
| 21:37:49 (log:207,212) | …069.339666 | …069.276447 | **−63.2ms** |
| 21:40:09 (log:224,229) | …209.148080 | …209.076507 | **−71.6ms** |

**4/4 모두 음수** = 첫 샘플은 startCapture 완료보다 ~70ms **앞서** 캡처됨. 판별 기준에 따르면 전부 **backlog/pre-roll 모드**이고, "무샘플 구간" 모드의 증거는 **하나도 없습니다**. 제 2라운드의 "WAV head 2.1초 결손" 추론을 **철회**합니다. 조정자의 파일길이 역산 반례가 맞습니다.

**D6 순위 조정**: 1위 → **공동 2위(P0 유지)**. 근거가 "실제로 큰 음수 offset이 자주 발생한다"에서 "발생 시 보정이 통째로 사라지고 알림도 없다"로 바뀌었기 때문입니다. 발현 빈도는 낮췄지만, 아래 2번 때문에 P0에서 내리지는 않습니다.

#### 2. 21:32의 `sys_offset = −1.374` 는 PTS 앵커가 아니라 **폴백 사다리**의 산물

결정적 사실: 21:32 세션(log:150-165)에는 `first_sample_host_at` 로그가 **없습니다** — PTS 로깅은 21:35부터 등장합니다. 따라서 그 실행의 앵커는 `_AUDIO_ANCHOR_ATTRS`(recorder.py:35)의 2순위인 `first_sample_at`, 즉 **콜백 도착 시각 21:32:04.984**입니다. startCapture는 21:32:02.856이었으므로 콜백은 2.128초 늦게 도착했고, 그 버퍼의 내용은 (backlog 모드이므로) 1~2초 과거였습니다. 영상 앵커(recordingDidStart) ≈ 21:32:03.61 이면 `3.61 − 4.984 = −1.374` 로 정확히 재현됩니다.

즉 **−1.374는 "무샘플 구간"의 증거가 아니라 D12(폴백 사다리)의 증거**입니다. PTS 앵커가 안정적(±12ms)이라는 관측과 모순되지 않습니다 — 그 실행은 PTS 앵커를 쓰지 않았습니다. 그리고 이 음수 offset은 곧바로 D6의 no-op 경로로 떨어졌습니다. 두 결함이 직렬로 연결되어 있고, 이것이 D6를 P0에 남기는 이유입니다. (조정자의 "필요 offset +2.84 / 잔차 −4.2초"는 플래시 간격 불규칙성 때문에 저도 신뢰하지 않습니다.)

#### 3. 최종 계획 (3자 합의안)

1. **`addRecordingOutput` → `startCapture` 순서 교정** — `continuous_screen_recorder.py:92-95, 341-370`. 2h. *없으면: 영상 t0가 스트림 t0보다 0.14~1.09s 늦어 모든 offset이 크게 유지되고 나머지 결함이 초 단위로 증폭됨.*
2. **폴백 사다리 차단** — `recorder.py:35, 291-297, 573-579`. PTS 앵커 실패 시 `first_sample_at`/`started_at`로 내려가지 말고 offset=0 + 경고, 그리고 `|offset| > 0.15` 이거나 폴백 발생 시 진단 플래그. 2h. *없으면: 21:32처럼 −1.374 같은 허위 offset이 조용히 적용됨.*
3. **`-ss`/`-itsoffset` → `atrim=start_sample`/`adelay:all=1`+`apad`, `normalize=0`+`alimiter`** — `recorder.py:309-314, 1007-1014, 1477, 1611-1624`. 4h. *없으면: 음수 offset 보정이 amix에서 소멸하고 50ms 데드존이 남음.*
4. **D7 이중 trim** — `recorder.py:935, 948`(2줄). 15m. *없으면: pause 사용 시 오디오가 첫 세그먼트 offset만큼 추가로 앞당겨짐.*
5. **D9 pause 순서 뒤집기** — `recorder.py:730-751`. 30m. *없으면: 세그먼트마다 영상이 오디오보다 길어져 누적 드리프트.*
6. **D8 세그먼트 concat 정규화 + 오디오/영상 세그먼트 길이 정합** — `continuous_screen_recorder.py:497-498`이 `recorder.py:1080-1115` 방식을 쓰도록. 1d. *없으면: pause 구간이 영상엔 남고 오디오엔 없어 초 단위 desync.*
7. **프로브 기반 `mic_latency_correction` 자동 반영 중단** — `app.py:839-845`, `sync_diagnostics.py:729-732`. 30m. *없으면: 스피커→공기→마이크 왕복 지연이 회의 오디오 보정값으로 승격됨.*
8. **영상 첫 프레임 PTS 앵커 도입** — `continuous_screen_recorder.py` 에 `SCStreamOutputTypeScreen` output 추가(PTS만 읽고 return). 1~2d. *없으면: 영상 t0가 wall clock 콜백 추정치로 남아 −40~−90ms 계통 편향이 영구화.*
9. **`_PCMFileWriter` PTS 회계 + gap 패딩 + 드리프트 카운터** — `system_audio.py:387-396`. 4h. *없으면: 장시간 녹화에서 ppm 드리프트/드롭이 무보고로 누적(1시간에 36~360ms).*
10. **D16 amix 라벨 명시 + `mic_echo_cancel` 도달 경로 복구** — `recorder.py:1477`, `app.py:1521-1530`. 1h. *없으면: mov에 오디오가 있으면 mic이 조용히 탈락, AEC 기능은 영구 미실행.*

#### 4. 단 하나의 근본 변경

> **싱크 계산에서 wall clock을 완전히 축출하고, 영상 t0와 오디오 t0를 같은 host-clock PTS로 측정해 그 차이를 샘플 단위 필터(`atrim`/`adelay`)로 적용한다.**

C의 문장과 **목표는 동일**(wall clock 축출), **수단은 다릅니다**: C는 단일 SCStream + SCRecordingOutput이 영상+sys를 mux하게 하고, 저는 두 스트림을 유지한 채 recording stream에 프레임 PTS 프로브를 붙입니다. 이유는 C 자신이 든 반대 근거입니다 — SCRecordingOutput의 오디오는 이 저장소에서 popping 때문에 이미 폐기됐고(`recorder.py:523-525`), `captureMicrophone` 조합의 mp4 손상 보고가 있습니다. 따라서 **단일 스트림 mux는 8번이 검증된 뒤의 2단계**로 두고, 1단계에서는 두 스트림 + 동일 PTS 도메인으로 오차를 프레임 1개 이하로 줄이는 것이 합의 가능한 형태라고 봅니다.


## [3R] 관점 C — 관측가능성·설계 대안

### 관점 C 3라운드: 최종 수렴

#### 1. 316ms 최종 입장 — **미결이며, 그것이 결론이다**

조정자 반례를 수용하고 2라운드 입장을 철회한다. backlog 모드는 버퍼 **지연 전달**이지 유실이 아니고, 타임라인 압축을 만들지 않는다. 유실 직접 증거 0건이므로 B의 메커니즘은 지지 근거를 잃었다.

그런데 조정자 데이터는 더 강한 것을 보여준다. 그 세션에서 **길이 역산 A0 ≈ 41.0**과 **PTS 역산 A0 ≈ 41.96**(첫 콜백 42.038 − 75ms)이 **약 0.9초 불일치**한다. WAV가 캡처 창보다 길 수는 없으므로, 이는 "첫 버퍼의 PTS가 WAV sample 0의 시각이 아니다"를 뜻한다 — 즉 내 1라운드의 **A2(τ_tap: 탭 PTS의 의미 미검증)** 가 살아 있고, 316ms는 그 계열의 잔차다.

따라서: A의 H2(기각, 물리 경로), B의 유실(철회), 남은 후보는 **A2(탭 PTS 의미) + V1/V2(영상 앵커)** — **셋 다 미관측**. 이것은 실패가 아니라 내 1라운드 논지의 직접 증거다: **네 개의 미관측량과 관측식 하나로는 어떤 가설도 판정할 수 없다.**

**실행 계획에 주는 변경 — 우선순위가 바뀐다.** 2라운드 P1의 "PTS 갭 로깅"을 더 강한 형태로 승격한다: 버퍼마다 `(pts, 누적 샘플수)`를 기록해 **PTS 역산 길이 vs 샘플수 역산 길이**를 비교한다. 이 한 곳의 계측이 A2·N_dropped·drift를 **동시에** 관측량으로 바꾸고, 0.9초 불일치를 즉시 판정한다. 교정(5b)보다 **계측이 먼저**다 — 원인 미결 상태에서 무음 패딩을 넣으면 잘못된 모델을 고착시킨다.

#### 2. 수렴점 확정 — 두 조건 모두 통과

**(a) sys.wav 소멸 문제: 비용 없음.** `pipeline.py:81,138-157`에서 `is_audio_only`는 `.wav` 입력만이고, screen 모드는 mp4를 `extract_audio`(`audio_extractor.py:67-80`)로 넘긴다. **STT는 이미 mp4에서 추출한다.** 게다가 `compress_and_merge`가 `audio_path`/`mic_path`를 삭제한다(`recorder.py:1578-1583`) — sys.wav는 애초에 파이프라인에 도달하지 않는다. 대안 1은 STT 품질에 무영향.

**(b) D13(마이크 PTS 도메인): 실재 위험, 다층 방어.** 문서 보장 없음을 인정한다. 방어: ① sys를 audio stream output으로 병행 수신해 `video/sys/mic` 세 PTS를 한 런에서 얻고 쌍별 차이의 크기·안정성으로 도메인 일치를 **검증**한다(불일치 시 값이 터진다). ② 최종 안전망은 대안 2 — 실제 회의에서 마이크는 스피커 소리를 주워 담으므로 mic↔sys 교차상관이 **어떤 시계 주장과도 무관하게** lag를 측정한다. 즉 D13이 참이어도 안이 무너지지 않는다.

#### 3. 최종 계획 (3자 합의용)

| # | 항목 | 위치 | 작업량 | 없으면 깨지는 것 |
|---|---|---|---|---|
| 1 | D7 이중 trim 제거 (`first_sys_offset`→0.0) | `recorder.py:936,948` vs `:1309-1310` | S | pause 사용 시 오디오가 offset만큼 두 번 잘림 |
| 2 | D8 `_concat_videos_normalized` screen 경로 연결 | `recorder.py:1080-1088`, `:900-953` | S | 영상에 pause 구간 frozen 잔존, 오디오는 제거 → 초 단위 |
| 3 | D9 pause 순서: 영상 pause를 최우선 | `recorder.py:730-751` | S | `_stop_mic` 10초 wait만큼 영상 tail 초과 |
| 4 | D5 포맷 전환 가드(중도 ASBD 변화 시 오류/재개) | `system_audio.py:387-396` | S | 48k→44.1k 시 8.8% 선형 드리프트 |
| 5 | **PTS 계측**: 버퍼별 `(pts, 누적샘플)` 로깅 + 길이 이중 역산 | `system_audio.py:387-396` | S | 316ms·A2·drift 영구 미결 |
| 6 | 콜백 견고성: de-interleave 벡터화, 락 분리, 전용 queue, 예외 노출 | `system_audio.py:330-337, 480-485, 589-594` | M | 지연 전달·드롭이 부하마다 재발 |
| 7 | `addRecordingOutput`을 `startCapture` 이전으로 | `continuous_screen_recorder.py:92-95, 327-339, 365` | M | 세그먼트 0의 V1이 0.14~1.09초 유지 |
| 8 | 첫 프레임 PTS 관측용 screen stream output (읽고 즉시 제거) | `continuous_screen_recorder.py`, `recorder.py:341-362` | M | resume 세그먼트 앵커 미식별, #7 검증 불가 |
| 9 | 프로브 유래 `mic_latency_correction` config 자동 반영 중단 | `app.py:820-851`, `sync_diagnostics.py:729-732` | S | 스피커 음향 지연 L이 타임스탬프 보정으로 영구 고착 |
| 10 | 데드존·트림 정밀화(±50ms 제거, `-c copy`→PCM 재인코딩) | `recorder.py:309-314, 1013` | S | 오차 예산 80ms 중 50ms를 설계적으로 낭비 |

이후(계측 결과 확정 후): 대안 1(`capture_audio=True`, captureMicrophone off) + 대안 2(교차상관 검증·drift 적합). 대안 3은 Swift 헬퍼로만, 예비.

#### 4. 단 하나의 근본 변경 — 판정

**A와 B는 사실상 같은 문장이다** (A=관측 대상 정의, B=적용 메커니즘). **내 안은 종류가 다르다**: A/B는 양을 *관측 가능하게* 만들고, 나는 영상↔sys 쌍에서 그 양을 *존재하지 않게* 만든다. 그러나 마이크가 남는 한 나도 PTS−PTS가 필요하다. 따라서 A/B가 **필요조건**, 내 mux가 **최종 단순화**다.

> **wall clock 뺄셈을 싱크 계산에서 폐기하고, 정렬은 오직 같은 host clock PTS 차이로 샘플 단위 적용하며(A·B), 정렬이 필요한 트랙 수 자체를 단일 스트림 mux로 최소화한다(C).**

단, 5번 계측이 A2를 해소하기 전까지 "PTS는 옳다"는 전제는 **검증 대상**이다 — 0.9초 불일치가 그것을 요구한다.


---

# 토론 종료 — 합의와 잔여 불일치

## 3자가 합의한 것

| 항목 | A | B | C |
|---|---|---|---|
| 싱크 계산에서 wall clock 축출, 정렬은 host clock PTS 차이로만 | 합의 | 합의 | 합의 |
| offset 적용을 `-ss`/`-itsoffset` → 샘플 단위 `atrim`/`adelay` 로 교체 | 합의 | 합의 | 합의 |
| `-itsoffset` + `amix` 는 no-op (음수 offset 소멸) | 최초 지적 | 독립 확인 | 합의 |
| ±50ms 데드존 제거 | 최초 지적 | 합의 | 합의 |
| `addRecordingOutput` 을 `startCapture` 이전으로 | 합의 | 합의 | 최초 지적 |
| 영상 첫 프레임 PTS 관측용 stream output 추가 | 최초 지적 | 합의 | 독립 제시 |
| D7 이중 trim / D9 pause 순서 / D16 amix 라벨 | 합의 | 최초 지적 | 합의(P0 승격) |
| D8 세그먼트 concat 정규화 미연결 | 3R에서 수용 | 최초 지적 | 합의(P0 승격) |
| `_PCMFileWriter` PTS 회계·계측 | 합의(H3) | 합의 | 합의(계측 우선) |
| 프로브 유래 `mic_latency_correction` 의 config 자동 반영 중단 | 합의 | 합의 | 최초 지적 |
| 프로브는 구조적 미식별 계측기 — 보정 근거로 쓸 수 없다 | 합의 | 합의 | 최초 지적 |
| 단일 스트림 mux(`capture_audio=True`)는 **검증 후 2단계** | 합의 | 합의 | 합의 |

## 토론 과정에서 철회된 주장

- **A의 H2** (316ms = 스피커 출력 지연 L): C가 신호 경로로 기각. 탭은 HAL 출력 믹스 지점에서 샘플을 취하고 `deviceCurrentTime` 도 같은 host time 도메인이므로, DAC 하류 지연 L 은 이미 찍힌 샘플의 위치를 바꿀 수 없다. A는 sys 경로에 대해 철회하고, "L 은 마이크 경로에서만 미상쇄로 남는다"로 위치를 정정했다.
- **A의 "프레임 PTS 관측이 1순위"**: A가 스스로 4~5순위로 강등. ±50ms 데드존과 AAC 프라이밍(21~44ms)을 먼저 제거해야 측정 가능한 이득이 난다는 이유.
- **A의 D8 반박**: 조정자 판정으로 기각, A가 전면 수용하고 오히려 실행 순위 1번으로 승격.
- **B의 "WAV 타임라인 압축이 잔차의 주원인"**: 로그 12개 세션에서 버퍼 처리 오류 0건을 확인하고 철회.
- **B의 "오디오 head 결손 2.1초"**: 조정자 반례(파일 길이 역산)로 철회. PTS 가 기록된 4개 세션은 전부 head 잉여(−63~−75ms)였다.
- **C의 "`addRecordingOutput` 순서는 Apple 문서가 보장한다"**: 재확인 결과 해당 레퍼런스에 Discussion 절이 없어, 근거 등급을 "문서 보장" → "WWDC24 샘플 관례 + 2차 출처"로 하향.
- **C의 "B의 버퍼 유실이 316ms 의 최유력 설명"**: 조정자 반례 수용 후 철회, "현재 계측으로 미결"로 수정.

## 종료 시점의 잔여 불일치

1. **316ms 의 정체는 미결.** A의 H2 기각, B의 버퍼 유실 철회 후 남은 후보는 C가 지적한 τ_tap(탭 PTS 의미 미검증)과 V1/V2(영상 앵커 오차)이며 **셋 다 미관측**이다. C는 21:25 세션에서 길이 역산 A0(≈41.0)과 PTS 역산 A0(≈41.96)이 약 0.9초 불일치한다는 점을 들어 "첫 버퍼의 PTS 가 WAV sample 0 의 시각이 아닐 수 있다"를 살아 있는 가설로 남겼다. 세 관점 모두 **계측을 먼저 넣어야 판정 가능**하다는 데 동의했다.
2. **"단 하나의 근본 변경"의 수단.** A·B는 두 스트림을 유지한 채 PTS 도메인을 통일하는 안, C는 단일 스트림 mux 로 정렬 대상 자체를 줄이는 안. C는 "A/B 가 필요조건, 내 mux 는 최종 단순화"라고 판정해 순서 문제로 정리됐다.
3. **오차 성분의 지배 관계.** C는 "이상치·분산은 오디오 경로, 정상 케이스의 ±30~60ms 편향은 영상 앵커(V1·V2)가 지배 — 어느 하나만 고쳐도 80ms 안에 안정적으로 들어가지 못한다"는 유보를 유지했다.
