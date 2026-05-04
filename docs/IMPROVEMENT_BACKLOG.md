# Improvement Backlog

작성일: 2026-05-04 (1.1.16 기준)

전체 코드 분석 결과 도출된 개선 항목. 우선순위 순. 각 항목은 독립적으로 처리 가능.

---

## 🔴 우선순위 높음 — 실제 사용자 영향 있음

### ~~[H1] 장기 녹화 시 ffmpeg subprocess stderr 미배수~~ ✅ 완료

- **위치**: `recorder.py:210-215` (`_start_mic`의 `subprocess.Popen`)
- **문제**: `stderr=PIPE`로 띄우고 초반 2초 폴링 후 녹화 종료까지 stderr를 한 번도 읽지 않음. macOS 파이프 버퍼(64KB) 도달 시 ffmpeg writer가 블로킹 → 1~2시간 녹화에서 발생 가능.
- **수정 내용**: 옵션 A 적용 — `_start_mic` 정상 진입 후 daemon drain 스레드 spawn (`mic-stderr-drain`), readline → /dev/null 흘려보내기. `_stop_mic`에서 join(timeout=2)으로 정리.
- **테스트**: `test_start_mic_spawns_stderr_drain_thread_when_stderr_pipe_open` (100KB stderr 흘려도 drain 정상 종료), `test_start_mic_skips_drain_thread_when_no_stderr_attribute` 추가

### ~~[H2] `except Exception:` 광범위 사용으로 실패 은폐~~ ✅ 부분 완료

- **위치**: 진단 결과 84개 중 진짜 사일런트 스왈로우(`pass`/단순 `return`) 9곳만 의미 있다고 판단
- **수정 내용 (1.1.16+)**:
  - `app.py:1187` 화면 권한 조회 실패 → `logger.debug(..., exc_info=True)` 추가
  - `app.py:1256` 마이크 권한 조회 실패 → 동일
  - `app.py:1694` 종료 시 녹화 중지 실패 → `logger.warning(..., exc_info=True)`
  - `app.py:1715` NSApplication Accessory 정책 실패 → `logger.warning(..., exc_info=True)`
  - `transcriber.py:509` CUDA empty_cache 실패 → `logger.debug(..., exc_info=True)`
  - `transcriber.py:515` MPS empty_cache 실패 → 동일
  - `system_audio.py:147` AVFoundation 임포트 실패 → `logger.debug(..., exc_info=True)`
  - `system_audio.py:153` AVCaptureDevice enumeration 실패 → `logger.warning(..., exc_info=True)`
- **의도적으로 미수정**:
  - `recorder.py:263` (mic stderr drain 내부) — backend drain은 silent가 맞음 (로깅 시 spam)
  - `system_audio.py:44` `_flog` — 로그 파일 쓰기 자체 실패라 logger 사용 시 재귀 위험
  - `transcriber.py:673` — 이미 `exc_info=True` 로깅 존재
  - `transcriber.py:907` — traceback을 result_queue로 부모 프로세스에 전달
- **남은 후속 작업 (별도 항목)**: `except Exception as e: logger.error("... %s", e)` 패턴 75건은 traceback 누락 → `logger.exception()` 또는 `exc_info=True` 추가 권장 (대량이라 별도 PR)

### ~~[H3] 데몬 스레드 8+개 join 없음~~ ✅ 완료

- **위치**: `app.py`의 9개 `threading.Thread(daemon=True)` spawn (validate/download/run-files/on-recording-stopped × 2 / start-bg × 2 / resume)
- **문제**: 종료 시 강제 중단 → 임시 파일/부분 mp4/wav/모델 잔존 위험
- **수정 내용**:
  - `_spawn_bg_thread(target, args, name)` 헬퍼 도입 — daemon 스레드를 `_bg_threads` 리스트에 등록 후 시작 (락으로 보호, dead 스레드 자동 prune)
  - `_join_bg_threads(timeout)` — graceful join, alive 스레드 카운트 반환
  - `_quit()` 에서 다운로드 cancel signal → bg join(timeout=3s) → hotkey stop → quit_application 순서
  - 9개 spawn 사이트를 모두 헬퍼로 전환
- **테스트 (4개 추가)**: spawn 등록/dead prune/timeout 내 join/timeout 초과 시 alive 카운트

---

## 🟡 우선순위 중간 — 유지보수성/안정성

### [M1] `app.py` 1723줄 / 83 메서드의 God Object

- **위치**: `app.py` 전체
- **문제**: UI / 권한 / 녹화 오케스트레이션 / 설정 I/O / 단축키 / STT 백엔드 선택 / 노트 생성 등 모두 한 클래스. 테스트 커버리지 7건에 그치는 핵심 원인.
- **수정 방향 (단계적)**:
  1. `PermissionManager` 분리 (screen/mic 권한 체크 + 결과 폴링)
  2. `ConfigStore` 분리 (load/save/validate, 변경 알림)
  3. `MenuController` 분리 (rumps 메뉴 항목 빌드/라벨 갱신)
  4. `RecordingCoordinator` 분리 (busy-guard + 모드 전환 + 파이프라인 트리거)
- **주의**: rumps의 `@rumps.clicked` 데코레이터는 메서드 바인딩 의존이라 분리 시 콜백 리바인딩 필요
- **검증 방법**: 각 단계마다 회귀 테스트 통과 + app.py 라인 수 감소

### [M2] 설정이 모듈 간 명시 주입 안 됨

- **위치**: `app.py`에서 `load_config()` 1회, `Recorder`/`Transcriber`/`Pipeline` 호출부에 미전달
- **문제**: 자식 모듈이 모듈 레벨 디폴트 / 매번 재로드 / 주입 안 받음 등 혼재. 런타임 변경 시 stale.
- **수정 방향**:
  - `Config` 객체(혹은 dataclass)를 명시적 생성자 파라미터로 주입
  - 자식 모듈에서 모듈 레벨 디폴트 제거
  - 변경 알림이 필요한 경우 옵저버 또는 매번 인자로 전달
- **검증 방법**: pytest로 임의 config 주입 가능해지는지

### [M3] 테스트 0개 모듈 6개 보강 — 진행 중 (4/6)

- **대상**:
  - ~~`pipeline.py` (316 lines) — STT→전처리→노트 핵심 흐름~~ ✅ — `tests/test_pipeline.py` (8개)
  - ~~`audio_extractor.py` (136 lines) — ffmpeg 추출/duration 파싱~~ ✅ — `tests/test_audio_extractor.py` (12개)
  - ~~`note_generator.py` (185 lines) — 마크다운 생성~~ ✅ — `tests/test_note_generator.py` (14개)
  - ~~`audio_preprocessor.py` (204 lines) — 노이즈/무음/정규화~~ ✅ — `tests/test_audio_preprocessor.py` (12개): `_energy_vad` 무음 입력 빈 결과/단일 음성 구간 감지/MERGE_GAP 이내 인접 구간 합치기, `_normalize_segments` 저음량 증폭/이미 정규화된 신호 클리핑 방지/MAX_GAIN 캡, `preprocess_audio` 전체 비활성화 시 단순 복사/VAD only 결과 길이 단축/순수 무음 입력 시 원본 fallback/stop_event cancel/progress callback/noisereduce 호출 검증
  - `system_audio.py` (592 lines) — SCStream + WAV 헤더 작성 (보류, 가성비 낮음)
  - `live_screen_writer.py` (227 lines) (보류, 가성비 낮음)
- **현황**: 테스트 43 → 100 (+57건; H1 +2, H3 +4, M3 +46, L1 +5)
- **남은 모듈**: `system_audio.py` / `live_screen_writer.py` — SCStream 모킹 부담 큼. 가성비 낮아 **장기 보류 권장**.

---

## 🟢 우선순위 낮음 — 잠재적 개선

### ~~[L1] `_with_software_video_encoder` 패턴 매처 fragile~~ ✅ 완료

- **위치**: `recorder.py` (이전 1019-1028)
- **문제**: 정확히 8개 토큰 리스트 매칭. 코덱 인자 추가/변경 시 침묵으로 깨짐.
- **수정 내용**:
  - `_HW_VIDEO_ENCODER_ARGS` / `_SW_VIDEO_ENCODER_ARGS` tuple 상수 도입 (단일 진실 소스)
  - `_hardware_video_codec_args()` / `_software_video_codec_args()` classmethod
  - `_with_software_video_encoder()` 매처를 sentinel 방식으로 재작성 — `-c:v hevc_videotoolbox` 위치만 매칭하고 그 뒤 비디오 인코더 옵션 그룹(`-q:v`/`-tag:v`/`-fps_mode`/`-preset`/`-crf`/`-pix_fmt`/`-b:v`)을 통째로 교체. 인자 추가/변경에 강함
  - 메인 병합 4곳 + concat 폴백 1곳 모두 헬퍼 호출로 일원화
- **테스트 (5개 추가)**: hw/sw 인자 형태, hw 블록 sentinel 매칭, hw 블록 없을 때 변경 없음, **미래에 hw 인자 그룹에 옵션 추가돼도 매처 깨지지 않음 회귀 테스트**

### ~~[L2] 브리틀 테스트 패턴~~ ✅ 완료

- **위치**: `tests/test_recorder_screen_mode.py`의 `test_concat_files_video_fallback_on_copy_fail`, `test_concat_files_audio_fallback_to_resample`
- **문제**: `attempt[0] == N` 호출 카운터 분기 + `assertEqual(attempt[0], 3)` 식 검증 → 폴백 단계 추가/제거 시 침묵으로 깨짐
- **수정 내용**: 행동 검증 방식으로 전환 — 각 호출의 cmd 토큰을 기록하고, 결과는 cmd 내용(`copy`/`hevc_videotoolbox`/`libx265`/`pcm_s16le`)으로 결정. 마지막에 폴백 체인이 모두 시도되었는지 cmd 내용으로 검증. 호출 횟수에 의존하지 않음.

### [L3] `cancellation.py`는 빈 껍데기 (3줄)

- **위치**: `cancellation.py` (예외 클래스 1개만)
- **문제**: 전반에 `stop_event`/subprocess kill 로직 분산. 통합 인프라 없음.
- **수정 방향**: `CancellationToken` 객체에 `stop_event` + 등록된 subprocess 리스트 + propagate 메서드 도입. 단, 도입 비용 vs 이득 검토 필요 (현재도 동작은 함)

---

## 🚫 분석 중 거짓/과장으로 판명나 제외한 항목 (참고용)

- ~~Recorder.stop() 락 미사용~~ → 사실 아님. `recorder.py:796`에서 `with self._lock:` 전체 감쌈
- ~~_start_mic 2초 폴링 중 stderr 블로킹~~ → 2초/64KB라 거의 불가능
- ~~PyObjC 델리게이트 retain cycle~~ → 가능성은 있으나 실측 누수 증거 미발견. 당장 보류

---

## 권장 처리 순서

손쉬운 것부터 단계적으로:

1. **[H1]** stderr drain (1~2시간) — 안정성 효과 즉시 가시화
2. **[H2]** `except Exception:` → `logger.exception` 변경 (반나절) — 디버깅 가능성↑
3. **[H3]** 데몬 스레드 graceful shutdown (반나절~1일)
4. **[M3]** `pipeline.py` 단위 테스트 (1일) — 이후 리팩토링 안전망
5. **[M2]** 설정 주입 패턴 (1~2일)
6. **[M1]** `app.py` 분할 (수일~수주, 단계적)
7. **[L*]** 여유 시간

각 작업은 독립 PR/커밋으로 분리 권장.
