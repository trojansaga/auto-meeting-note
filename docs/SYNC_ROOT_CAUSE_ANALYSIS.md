# A/V 싱크 근본원인 분석 — 정리본

- 작성일: 2026-08-20
- 대상: 화면 녹화 모드(`Recorder.start_screen_recording`)의 오디오/영상 싱크 오차
- 방법: 서로 다른 관점의 에이전트 3개(시계 도메인 이론 / 구현 감사 / 관측가능성·설계 대안)가 독립 분석 → 상호 반박 → 조정자 판정 → 수렴. 편차 재측정 대신 **구현과 이론**을 근거로 분석했다.
- 전체 토론 기록: [SYNC_ROOT_CAUSE_DISCUSSION_LOG.md](SYNC_ROOT_CAUSE_DISCUSSION_LOG.md)

---

## 1. 한 줄 결론

**싱크를 "두 캡처의 시작 시각을 각각 `time.time()`으로 찍어 뺀 값"으로 정의한 것이 근본 원인이다.** 그 뺄셈에는 물리적 시각차가 아니라 파이썬 콜백 디스패치 지연이 들어가고, 그렇게 얻은 값을 적용하는 경로마저 음수 offset을 조용히 버린다. 따라서 앵커를 아무리 정밀화해도 오차의 상한이 존재하지 않는다.

**근본 해결책(3자 합의):**

> 싱크 계산에서 wall clock을 완전히 축출하고, 영상 t=0과 오디오 t=0을 **같은 host clock의 CMSampleBuffer PTS**로 측정해 그 차이를 **샘플 단위 필터(`atrim`/`adelay`)**로 적용한다. 정렬이 필요한 트랙 수 자체는 이후 단일 스트림 mux로 줄인다.

---

## 2. 왜 지금 구조로는 안 되는가 (이론)

### 2.1 필요한 양과 관측되는 양이 다르다

싱크를 맞추려면 알아야 하는 값은 하나다.

```
Δ = (mp4 video pts 0 의 실제 시각) − (WAV sample 0 의 실제 시각)
```

현재 코드가 실제로 계산하는 값은 이것이다(`recorder.py:615`).

```
Δ̂ = time.time()@recordingDidStart콜백 − wall변환(첫 오디오 버퍼 PTS)
```

좌변 두 항의 **오차원이 서로 다르다**. 앞항은 소프트웨어 콜백 도착 시각(디스패치 큐 지연 포함), 뒷항은 하드웨어 타임스탬프다. 오차가 상쇄되지 않고 그대로 남는다. 이것이 실측에서 확인된 두 사실의 직접적 귀결이다.

- `startCapture` 완료 시각은 mp4 첫 프레임보다 0.14~1.09초 이르다 → 실행마다 편차가 큰 **랜덤 항**
- `recordingDidStart`는 −40~−90ms로 일관 → 제거되지 않는 **계통 편향**

### 2.2 관측 불가능한 잠재변수가 최소 4개다

| 기호 | 정체 | 관측 가능? |
|---|---|---|
| V1 | recordingDidStart 콜백 ↔ mp4 pts 0 사이의 지연 | 불가 (프레임 콜백이 없어 영상 PTS를 한 번도 보지 않음) |
| V2 | 컴포지팅 지연 + 프레임 그리드 양자화(30fps → 최대 33ms, 단측 편향) | 불가 |
| V3 | 세그먼트별 타임라인 재기점화(pause/resume 시 세그먼트마다 새로 발생) | 불가 |
| τ_tap | 오디오 탭 PTS가 정확히 어느 지점의 시각인지 (문서 미규정) | 불가 |

실제 오차는 이들의 합이며, 산출물이나 로그에서 어느 하나도 분리할 수 없다. **`recordingDidStart`를 앵커로 쓰는 방식의 최선 정확도는 ±40~60ms이고, 상한은 존재하지 않는다** — 콜백 지연을 위에서 묶어주는 메커니즘이 프레임워크에 없기 때문이다.

### 2.3 진단 프로브로는 원인을 판정할 수 없다 (구조적 미식별)

프로브가 측정하는 값을 전개하면 미지수가 여럿인데 관측식은 하나다.

```
m = (t_click + τ_tap) − (t_flash + δ_disp + q_frame)
    미지수: Δ, τ_tap, δ_disp, q_frame  →  채널 1개, 미지수 4개
```

`_probe_emission_skew`는 이미 아는 부분(`click_started_at − flash_started_at`)만 뺀다. τ_tap·δ_disp·q_frame은 그대로 남는다. 따라서 **프로브로 앵커를 보정하는 루프는 원리적으로 수렴하지 않는다.** 실측 +17ms/−32ms는 참값이 아니라 프로브의 잡음 바닥(33ms 프레임 양자화 + 컴포지팅 지연)과 구별되지 않는 값이다.

이 판정에는 즉시 조치할 귀결이 하나 있다. 프로브 클릭은 스피커→공기→마이크를 지나므로 마이크 경로에는 음향 왕복 지연(내장 30~150ms, Bluetooth 100~300ms)이 실린다. 그런데 `recommend_sync_adjustments`(`sync_diagnostics.py:729-732`)가 바로 그 값을 `mic_latency_correction_seconds`로 산출하고 `app.py:820-851`이 `config.yaml`에 영구 기록한다. **계측기의 미식별 편향이 실제 회의 오디오의 타임스탬프 보정값으로 승격되는 경로다.** 사람이 말할 때 마이크 경로에는 스피커 지연이 없으므로, 이 보정은 실제 회의에서 마이크를 과보정한다.

### 2.4 앵커가 맞아도 적용 경로가 값을 버린다

앵커 논쟁과 무관하게 확정적인 결함들이다.

| 결함 | 위치 | 무슨 일이 생기는가 |
|---|---|---|
| 음수 offset이 `amix`에서 소멸 | `recorder.py:309-314`, `:1618` | ffmpeg `af_amix`는 출력 PTS를 **첫 번째 입력에서만** 취하고 나머지는 FIFO에 그냥 쌓는다. 입력별 PTS 정렬도, 헤드 무음 삽입도 없다. 즉 `-itsoffset`이 **완전한 no-op**이다 |
| 마이크 유무에 따른 비대칭 | `recorder.py:1611-1624` vs `:1645-1652` | `has_sys and has_mic`는 amix를 타서 no-op, `has_sys and not has_mic`는 `-map` 직결이라 동작. **같은 offset이 마이크 유무에 따라 다르게 해석된다** |
| ±50ms 데드존 | `recorder.py:309-313`, `:1009-1012` | 허용치 80ms 시스템에서 오차 예산의 62%를 설계적으로 포기. 그 위에 AAC 인코더 프라이밍(1024~2112 샘플 = 21~44ms)이 얹힌다 |
| `_trim_wav`가 음수 offset을 버림 | `recorder.py:1007-1014` | `offset <= 0.05`면 `shutil.copy2`. 무음 prepend가 없어 세그먼트 경로에서 음수 보정이 사라진다 |
| D7 이중 trim | `recorder.py:935`, `:948` | `_concat_screen_sys_segments`가 세그먼트별 offset을 이미 trim했는데 `stop()`이 첫 세그먼트 offset을 반환해 `-ss`로 **한 번 더** 자른다. 같은 파일의 `_concat_segments`는 올바르게 `0.0`을 반환(`:1309-1310`) — **자기모순** |
| D8 concat 정규화 미연결 | `recorder.py:1080-1088` | 저자 자신의 docstring: "SCRecordingOutput은 Mach time 기반 PTS를 기록해 `-c copy` 단순 결합 시 세그먼트 사이에 **일시정지 시간만큼 frozen 구간**이 생긴다." 우회 함수 `_concat_videos_normalized`를 만들어 뒀으나 screen 경로에서 **도달 불가**. 영상엔 pause가 남고 오디오엔 제거되어 pause 1회당 그 길이 전체가 desync |
| D9 pause 순서 | `recorder.py:730-751` | sys 정지 → `_stop_mic()`(**최대 10초 대기**, `:507`) → 그 다음 영상 pause. 세그먼트 tail이 영상만 길어지고 이를 보정하는 코드가 없다(offset은 head만 다룸) |
| D16 무라벨 amix | `recorder.py:1477` | 입력 3개인데 `amix=inputs=2`에 라벨이 없어 자동 선택. mov에 오디오가 있으면 mic이 조용히 탈락 |
| 폴백 사다리 | `recorder.py:35`, `:573-579` | PTS 앵커 실패 시 콜백 도착 시각 → `started_at`으로 내려간다. 실제로 이 경로에서 `sys_offset = −1.374`가 산출된 세션이 있고, 그 값은 곧바로 위의 no-op 경로로 떨어졌다 |

### 2.5 WAV 타임라인에 불변식이 없다

`_PCMFileWriter.write_sample_buffer`(`system_audio.py:387-396`)는 PTS를 쓰지 않고 PCM을 무조건 append하며, 콜백 예외는 `logger.debug`로만 삼켜진다(`:483-485`). 따라서

- 버퍼 1개(≈21ms) 유실마다 이후 전체 오디오가 영구히 앞으로 당겨진다(타임라인 압축) — 앵커를 완벽히 맞춰도 복구 불가
- WAV 헤더는 "48000 샘플 = 1초"라고 선언하지만 실제 캡처 레이트는 오디오 디바이스 클럭이다. 10~100ppm 어긋나면 **1시간에 36~360ms** 누적. 마이크는 또 다른 디바이스 클럭이라 독립적으로 drift
- 녹화 중 출력 디바이스 전환으로 샘플레이트가 48k→44.1k가 되면 헤더/내용 불일치로 **8.8% 선형 드리프트**(1시간에 5분). 사후 스칼라 offset으로는 원리적으로 복구 불가

지금까지의 실측은 모두 수 초짜리 프로브 녹화였으므로 이 항들이 관측에 나타나지 않았을 뿐이다.

### 2.6 구조적 원인 하나 더 — 호출 순서

`ContinuousCaptureController.start()`는 `start_stream()`(= `startCapture` 완료 대기) → `_start_new_segment()` → `addRecordingOutput_error_` 순서다(`continuous_screen_recorder.py:92-95`, `:365`). WWDC24 샘플과 커뮤니티 문서의 관례는 **`addRecordingOutput`을 `startCapture` 이전에** 호출하는 것이다(Apple 레퍼런스에 규범적 Discussion 절은 없어 근거 등급은 "관례"). 즉 실측 사실(첫 프레임 편차 0.14~1.09초)은 버그의 증상이 아니라 **이 순서의 귀결**이다. 순서를 고치면 offset 자체가 수축하고, 나머지 결함의 노출도 함께 줄어든다.

---

## 3. 실행 계획 (3자 합의)

앞 단계가 뒤 단계의 전제가 되도록 정렬했다. 1~4단계만으로도 pause/resume 사용 시의 초 단위 오차와 음수 offset 소멸이 사라진다.

### 1단계 — 확정적 버그 (반나절, 위험 거의 없음)

| # | 항목 | 위치 | 없으면 깨지는 것 |
|---|---|---|---|
| 1 | D7 이중 trim 제거 (다중 세그먼트에서 `first_sys_offset`/`first_mic_offset` → `0.0`) | `recorder.py:935`, `:948` | pause 사용 시 오디오가 첫 세그먼트 offset(0.1~1.0초)만큼 두 번 잘린다 |
| 2 | D9 pause 순서 역전 (영상 세그먼트를 먼저 닫고 오디오 정지) | `recorder.py:730-751` | `_stop_mic` 대기 시간만큼 영상 tail이 초과되고 세그먼트마다 누적 |
| 3 | D16 amix 입력 라벨 명시 | `recorder.py:1477` | mov에 오디오가 있으면 mic이 조용히 탈락 |
| 4 | 프로브 유래 `mic_latency_correction_seconds`의 config 자동 반영 중단(로그 전용으로 격하) | `app.py:820-851`, `sync_diagnostics.py:729-732` | 미식별 음향 왕복 편향이 프로덕션 상수로 영구 고착 |

### 2단계 — offset 적용 경로 정직화 (반나절)

| # | 항목 | 위치 |
|---|---|---|
| 5 | `-ss`/`-itsoffset` 폐기 → 필터그래프에서 샘플 단위로 처리. ±50ms 데드존 제거 | `recorder.py:309-314`, `:1611-1624`, `:1466-1483` |
| 6 | `_trim_wav`도 동일 방식(음수 offset은 무음 prepend, `-c copy` → PCM 재인코딩) | `recorder.py:1007-1014` |

합의된 필터그래프:

```
offset > 0 (오디오가 먼저 시작 → 앞을 버림)
    [N:a]atrim=start_sample=<round(offset*rate)>,asetpts=N/SR/TB[aN]
offset < 0 (오디오가 늦게 시작 → 앞에 무음 삽입)
    [N:a]adelay=<round(-offset*rate)>S:all=1[aN]

[a1][a2]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mx];
[mx]alimiter=limit=0.95[aout]
```

`adelay`는 `S` 접미사로 **샘플 단위** 지연이라 ms 반올림이 없다. `apad`는 꼬리 패딩 필터이므로 헤드 지연에 쓸 수 없다(오답). `normalize=0` + `alimiter`는 싱크와 무관하지만, `normalize=1`은 활성 입력 수가 바뀔 때 이득이 점프해 dropout 구간에 레벨 계단을 만든다.

**갱신 필요한 테스트**: `tests/test_capture_sync.py`의 `-ss`/`-itsoffset` 인자 단정을 `filter_complex` 단정으로 재작성. 그리고 **음수 offset 케이스 테스트를 신규 추가**해야 한다 — 현재 이 케이스를 검증하는 테스트가 없어서 no-op 결함이 살아남았다.

### 3단계 — 관측 가능하게 만들기 (반나절)

| # | 항목 | 위치 |
|---|---|---|
| 7 | 버퍼별 `(PTS, 누적 샘플수)` 로깅 + 종료 시 **PTS 역산 길이 vs 샘플수 역산 길이** 비교 | `system_audio.py:387-396` |
| 8 | 콜백 예외를 `logger.error`로 승격, PTS 확정을 write **성공 이후로** 이동 | `system_audio.py:472-485` |
| 9 | 폴백 사다리 차단 — PTS 앵커 실패 시 콜백 시각으로 내려가지 말고 offset=0 + 경고 | `recorder.py:35`, `:573-579` |

**계측이 교정보다 먼저다.** 원인이 미결인 상태에서 무음 패딩을 넣으면 잘못된 모델을 고착시킨다. 이 한 곳의 계측으로 버퍼 유실·클럭 drift·τ_tap이 **동시에** 관측량이 되고, 아래 §4의 미결 쟁점이 한 번의 세션 재분석으로 판정된다.

### 4단계 — 앵커를 PTS 도메인으로 통일 (1~2일)

| # | 항목 | 위치 |
|---|---|---|
| 10 | `addRecordingOutput`을 `startCapture` **이전으로** | `continuous_screen_recorder.py:92-95`, `:327-339`, `:365` |
| 11 | `SCStreamOutputTypeScreen` output을 추가해 **첫 프레임 PTS만 읽고 즉시 제거** → 영상 t=0 앵커로 사용 | `continuous_screen_recorder.py`, `recorder.py:341-362` |
| 12 | `offset = firstVideoPTS_host − firstAudioPTS_host` (wall clock 변환 없이 PTS끼리) | `recorder.py:365-368`, `:615` |

10번은 세그먼트 0만 구제한다. pause/resume은 라이브 스트림에 recording output을 중도 추가하므로 세그먼트마다 같은 문제가 되살아나며, 11번이 그것과 검증을 담당한다. 또한 순서 관례의 근거 등급이 "문서 보장"이 아니므로 **가정을 검증할 수단(11번)이 오히려 더 필요하다.**

이론적 정확도 상한: 프레임 그리드 양자화 ±16.7ms(오디오는 샘플 단위). 상시 프레임 콜백이 없으므로 성능 비용은 사실상 0이다.

### 5단계 — 세그먼트 타임라인 (1일)

| # | 항목 | 위치 |
|---|---|---|
| 13 | D8: 영상 세그먼트 concat을 PTS 재기준화 경로(`setpts=PTS-STARTPTS` 재인코딩)로 교체 | `continuous_screen_recorder.py:173-208` → `recorder.py:1080-1115` 방식 |
| 14 | 세그먼트별로 영상 길이를 재고 오디오를 `apad`/절단으로 길이 정합시킨 뒤 concat | `recorder.py:1188-1215` |

### 6단계 — 이후 (계측 결과 확정 후 재평가)

| # | 항목 | 비고 |
|---|---|---|
| 15 | `_PCMFileWriter` PTS 정렬 기록(gap → 무음 삽입, 초과 → 절단) + 실효 샘플레이트 산출 후 `aresample` 보정 | 장시간 녹화용. 3단계 계측 결과가 모델을 확정한 뒤에 |
| 16 | 콜백 견고성: 순수 Python de-interleave(`system_audio.py:330-337`) 벡터화, system/mic **공유 락** 분리(`:480-481`), `sampleHandlerQueue`에 전용 dispatch queue 지정(`:589-594`) | 15번의 전제조건. 48kHz stereo에서 초당 ~10만 회 Python 슬라이스 + GIL이 탭 드롭의 기계적 원인 |
| 17 | 단일 스트림 mux(`capture_audio=True`, `captureMicrophone` off, 마이크는 별도 유지) | 영상↔sys 정렬이 **구성적으로** 참이 되어 뺄셈이 소멸. STT는 이미 mp4에서 오디오를 추출하므로(`pipeline.py:150`) sys.wav 제거는 무영향. 위험: v1.1.13 popping 회귀(미검증), 관측점이 마이크로 이전 |
| 18 | 참조 오디오 트랙 + 교차상관으로 자기검증 및 drift 적합 | 시변 잠재변수를 런 불변 상수 1개(AAC 프라이밍)로 환원. 17번의 안전한 A/B 하네스 역할 |
| 19 | AVAssetWriter 직접 mux | 원리적으로 가장 깨끗하나 PyObjC로는 위험(GIL, 백프레셔, 파일 무효화). Swift 헬퍼로 분리해야 하며 예비안 |

---

## 4. 판정된 것과 미결인 것

### 판정된 것

- **`recordingDidStart`가 `startCapture` 완료보다 나은 앵커라는 것은 맞다** — 그러나 둘 다 wall clock 콜백이므로 정확도 상한이 없다. 앵커 선택은 문제의 해결이 아니라 완화였다.
- **`-itsoffset` + `amix`는 no-op이다** — ffmpeg `af_amix` 소스로 확인. 마이크 유무에 따라 동작이 갈리는 비대칭도 확인.
- **D7·D8·D9·D16은 앵커와 무관하게 확정적이다** — 특히 D8은 저장소 자신의 docstring이 현상을 기록하고 있고 우회 함수가 죽은 코드로 남아 있다. pause/resume을 한 번이라도 쓰면 초 단위로 틀어진다.
- **프로브는 구조적으로 미식별이며, 그 편향이 config에 기록되는 경로가 살아 있다** — 즉시 차단 대상.
- **버퍼 유실의 직접 증거는 없다** — 로그 12개 세션에서 오디오 버퍼 처리 오류 0건. 다만 감지 수단이 없다는 사실은 그대로다.
- **PTS 앵커는 안정적이다** — PTS가 기록된 4개 세션에서 `first_sample_host_at − startCapture`가 −67.3 / −74.6 / −63.2 / −71.6ms, 산포 11ms. 병리적 2.1초 세션들은 PTS 로깅 이전이었다.

### 미결인 것

- **316ms의 정체.** 스피커 출력 지연 가설은 신호 경로로 기각(탭은 HAL 출력 믹스 지점에서 취하고 `deviceCurrentTime`도 같은 host time 도메인이므로 DAC 하류 지연이 이미 찍힌 샘플 위치를 바꿀 수 없다), 버퍼 유실 가설은 증거 부재로 철회. 남은 후보는 τ_tap(탭 PTS 의미)과 영상 앵커 오차이며 **둘 다 미관측**이다. 한 세션에서 길이 역산 A0와 PTS 역산 A0가 약 0.9초 불일치하는 점이 τ_tap 가설을 살려 둔다.
- 이 미결 자체가 §2.3의 결론을 확증한다. **3단계 계측(7번)이 들어가기 전에는 어떤 가설도 판정할 수 없고, 따라서 어떤 보정값도 적용해서는 안 된다.**

### 오차 성분에 대한 유보

이상치와 실행 간 분산은 오디오 경로에서, 정상 케이스의 ±30~60ms 계통 편향은 영상 앵커(V1·V2)에서 온다는 견해가 유지됐다. 즉 **어느 한쪽만 고쳐도 허용치 80ms 안에 안정적으로 들어가지 못한다** — 1~4단계를 함께 넣어야 한다.

---

## 5. 왜 "자기검증 가능성"이 설계 기준인가

이 앱은 메뉴바 백그라운드 도구다. 사용자는 진단 모드를 켜지 않고, 싱크 회귀는 누군가 영상을 실제로 볼 때까지 보이지 않는다. 편향이 미지인 계측기로 맞춘 캘리브레이션 상수에 정확성이 의존하는 설계는 새 하드웨어(Bluetooth 헤드셋, 120Hz 외장 디스플레이, 다른 Mac)에서 **조용히** 무너진다.

- 구성적으로 옳은 설계(PTS 정렬, 단일 스트림 mux)는 런마다 검증이 필요 없다.
- 측정하는 설계(교차상관)는 스스로 검증한다.
- 프로브로 캘리브레이션하는 설계만은 어느 쪽도 아니다 — 유일한 검증 수단이 검증 대상과 얽혀 있다.
