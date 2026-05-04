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

### [H3] 데몬 스레드 8+개 join 없음

- **위치 (app.py 후보)**:
  - line 330 `_validate_openai_model`
  - line 493 `_download_model`
  - line 1180, 1333, 1379, 1392, 1438, 1444 등
- **문제**: `daemon=True`로 백그라운드 작업이 앱 종료 시 강제 중단됨. 임시 파일/부분 다운로드 모델 잔존 가능.
- **수정 방향**:
  - `atexit.register()` + `threading.Event` 기반 graceful shutdown
  - 모델 다운로드 같은 긴 작업은 명시적 cancel + 임시 파일 cleanup
  - 또는 `concurrent.futures.ThreadPoolExecutor`로 통합 + `executor.shutdown(wait=True, timeout=N)`
- **검증 방법**: 다운로드 진행 중 앱 종료 → 임시 파일 잔존 확인

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

### [M3] 테스트 0개 모듈 6개 보강

- **대상**:
  - `audio_extractor.py` (136 lines) — ffmpeg 추출/duration 파싱
  - `audio_preprocessor.py` (204 lines) — 노이즈/무음/정규화
  - `note_generator.py` (185 lines) — 마크다운 생성
  - `pipeline.py` (316 lines) — STT→전처리→노트 핵심 흐름 ← **가장 우선**
  - `system_audio.py` (592 lines) — SCStream + WAV 헤더 작성
  - `live_screen_writer.py` (227 lines)
- **수정 방향**:
  - 합성 오디오(numpy로 만든 sine wave WAV)로 audio_extractor/preprocessor 통합 테스트
  - `pipeline.py`는 단계별 mock으로 cancellation/error path 단위 테스트
  - `system_audio.py`는 SCStream 모킹 부담 커서 후순위
- **목표**: 테스트 43개 → 60개+

---

## 🟢 우선순위 낮음 — 잠재적 개선

### [L1] `_with_software_video_encoder` 패턴 매처 fragile

- **위치**: `recorder.py:1019-1028`
- **문제**: 정확히 8개 토큰 리스트 매칭. 코덱 인자 추가/변경 시 침묵으로 깨짐. (1.1.16에서 코덱 변경 시 잠재 위험)
- **수정 방향**: `_videotoolbox_video_codec_args()` 헬퍼로 중앙화하고, 폴백 시 슬라이스 위치를 헬퍼가 반환

### [L2] 브리틀 테스트 패턴

- **위치**: `tests/test_recorder_screen_mode.py` 등에서 `attempt[0] == N` 카운트 의존
- **문제**: 폴백 단계 추가/제거 시 침묵으로 깨짐
- **수정 방향**: 결과 파일 내용/메타로 행동 검증 (예: "reencoded" 바이트 vs 호출 횟수)

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
