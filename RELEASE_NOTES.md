# Release Notes

## 1.1.18

- 회의록 생성 실패 시 원인을 알 수 없던 문제 수정
  - claude CLI 로그인 만료 등으로 실패하면 종료 코드만 남고 실제 오류 메시지(`Not logged in · Please run /login`)가 사라지던 문제 수정 — CLI가 stdout(JSON)으로 알려주는 원인을 그대로 로그·알림에 노출
  - 인증 만료처럼 재시도해도 결과가 같은 오류는 3회 재시도하지 않고 즉시 중단하며, `claude` 실행 후 `/login` 하라는 조치 안내를 함께 표시
- 오디오 트랙이 없는 영상 처리 시 `ffmpeg 실행 실패 (exit code 234)` 만 남던 문제 수정
  - 녹화 종료 직후 오디오 병합이 실패해 영상만 저장된 파일임을 명확히 안내(`오디오 트랙이 없습니다 …`)하고, 남아 있는 `_sys.wav`로 재병합할 수 있음을 알림
  - 그 외 ffmpeg 실패도 종료 코드 대신 stderr 마지막 줄을 함께 보고
- 녹화 종료 직후 오디오 병합이 일시적 자원 부족(`[Errno 35] Resource temporarily unavailable`)으로 실패해 음성이 유실되던 문제 수정
  - 병합 프로세스 생성 실패 시 2초 간격으로 최대 3회 재시도
  - 끝내 실패해도 원본 `_sys.wav`/`_mic.wav`를 삭제하지 말라고 안내해 수동 재병합 경로 보존

## 1.1.17

- 녹화 중임을 한눈에 알 수 있도록 시각 표시기 추가
  - 메뉴바 타이머(`● REC 00:00`)를 빨간색으로 점멸시켜 가독성 향상
  - 화면 전체 가장자리에 빨간 글로우 테두리를 표시 (모든 디스플레이 지원, 풀스크린/메뉴바 위에도 표시)
  - 가장자리에서 안쪽으로 자연스럽게 사라지는 그라데이션 + 사인 곡선 기반의 부드러운 펄스(호흡) 애니메이션, 최대 불투명도 50%로 시야 방해 최소화
  - 테두리 오버레이는 `NSWindowSharingNone`으로 설정해 ScreenCaptureKit 녹화 결과물에는 포함되지 않음 (사용자 화면에만 표시)
  - 점멸/오버레이 생성 실패 시에도 녹화 자체에는 영향이 없도록 방어 처리

## 1.1.16

- 화면 녹화 영상의 파일 크기를 줄이기 위해 인코딩 파이프라인을 H.264 → HEVC 로 전환
  - 메인 병합/세그먼트 합치기/PTS 정규화 모두 `hevc_videotoolbox -q:v 40 -tag:v hvc1` 우선 사용, 실패 시 `libx265 -preset medium -crf 28 -tag:v hvc1` 으로 폴백
  - 모든 비디오 재인코딩 단계에 `-fps_mode passthrough` 추가해 원본 PTS 그대로 보존, 코덱 변경에 따른 싱크 회귀 차단
  - macOS QuickTime/Finder 미리보기·썸네일 호환을 위해 `hvc1` 태그 적용
- 캡처 해상도를 메인 디스플레이 풀픽셀의 75%(약 4K UHD급)로 다운스케일해 추가 용량 절감 (HEVC 매크로블록 정렬 위해 짝수 보정)
- 결과적으로 1시간 5K 일반 회의 기준 약 3.5~5.0 GB → 0.85~1.5 GB 수준으로 감소

## 1.1.15

- 화면 녹화 시 음성이 영상보다 빠르게 들리고 튀는 소음이 들어가던 문제 수정
  - 시스템 오디오를 `SCRecordingOutput`이 mp4에 직접 인코딩하던 경로 대신, `SystemAudioCapture`(SCStream raw sample → WAV)로 별도 캡처해 audio quality 보존 (1.1.13 검증 경로 복원)
  - macOS 15+에서는 마이크도 `SystemAudioCapture`의 SCStream으로 캡처해 ffmpeg subprocess 시작 지연으로 인한 마이크 timing 어긋남 제거
  - 화면 녹화 sync anchor를 `SCStream.startCaptureWithCompletionHandler_` 콜백 시각(`stream_capture_started_at`)으로 변경해 mp4 video time 0과의 정렬 정확도 개선
  - mp4 + 마이크 amix 필터에 `normalize=1` 적용해 합산 시 클리핑/튐 방지
  - 마이크 `_start_mic`을 WAV 파일 첫 샘플 기록 시점 폴링으로 변경해 ffmpeg avfoundation 초기화 지연 보정
- AAC 인코딩을 `192k @ 48kHz`로 명시해 병합 단계 인코딩 품질 향상
- `ContinuousScreenRecorder`에 `capture_audio` 파라미터 추가, 화면 녹화 모드에서는 영상만 캡처하도록 분리

## 1.1.14

- 일시정지 시간을 메뉴바 타이머 경과 시간에서 제외해 일시정지 중 카운트가 멈추도록 수정
- 일시정지/재개 후 합쳐진 화면 녹화 영상에서 정지 구간이 보이던 문제 수정 (각 세그먼트의 PTS를 0부터 재계산해 재인코딩하는 정규화 합치기 적용)
- 합치기 실패해도 다음 녹화가 정상적으로 시작되도록 `stop()` 상태 초기화 안정성 보강
- `export_dir` 설정값에 쉘 이스케이프(`\ `, `\~`)가 들어 있어도 자동으로 정규화해 회의록 내보내기가 정상 동작하도록 수정
- `build_app.sh`가 자체 서명 인증서(`AutoMeetingNote Local`)를 사용하도록 변경해 화면 녹화 등 권한이 매 빌드마다 초기화되지 않도록 함
- Naver AI Gateway를 회의록 생성 AI 공급자로 추가 (OpenAI와 전환 가능)
- 메뉴바에서 공급자 전환, 모델명 변경, Naver API 키 입력을 바로 할 수 있도록 `회의록 AI` 메뉴 추가
- Naver AI Gateway 및 Bedrock Claude 등 `temperature`를 지원하지 않는 모델에서는 해당 파라미터를 생략하도록 조정
- `BadRequestError(400)` 발생 시 불필요한 재시도 없이 즉시 오류 처리하도록 변경
- 한국어 이름이 포함된 오디오 입력 장치를 나열할 때 ASCII 인코딩 오류 수정
- `config.yaml`에 공백이 백슬래시 이스케이프(`\ `)로 저장된 경로를 복사 대상으로 찾지 못하던 문제 수정

## 1.1.13

- 화면 녹화 A/V 싱크 보정 기준을 `SCRecordingOutput` 시작 콜백보다 더 이른 실제 `SCStream` 캡처 시작 시각으로 조정해 소리가 약간 뒤로 밀리던 현상을 추가 완화

## 1.1.12

- 파일 처리(STT/회의록 생성) 중 다른 녹화/녹음이 끝나면 회의록 생성 확인창 대신 안내 팝업을 표시하고 종료하도록 변경
- 화면 녹화의 오디오 싱크 보정을 실제 시스템 오디오/영상 캡처 시작 시각 기준으로 계산하도록 수정
- `LiveScreenWriter`의 ScreenCaptureKit 버퍼 설정을 보강해 화면 녹화가 튀는 현상을 완화
- 시스템 오디오와 마이크를 함께 녹음할 때 오디오 믹싱 정규화를 적용해 튀는 소리와 클리핑을 줄이도록 조정

## 1.1.11

- `녹화/녹음 옵션` 메뉴에 `마이크 입력` 하위 메뉴 추가
- 마이크 입력을 맥북/현재 디바이스 또는 iPhone 마이크로 선택할 수 있도록 지원
- 기존 iPhone 마이크 자동 대체 로직을 명시 선택 시 iPhone을 사용할 수 있도록 변경

## 1.1.10

- 알림 런타임 `Info.plist`를 실제 Python 런타임 경로에 생성하도록 수정
- `rumps` 알림 실패 시 `osascript` 전에 AppKit 네이티브 알림으로 재시도하도록 변경
- 알림 표시 주체가 `Script Editor`가 아닌 `AutoMeetingNote`로 보이도록 보강

## 1.1.9

- 최종 회의록 Markdown 파일명 맨 앞에 `(자동회의록)` 접두사를 붙이도록 변경

## 1.1.8

- Apple Speech 실행 전에 compatible audio format을 선검사하고 `prepareToAnalyze`를 먼저 수행하도록 변경
- Apple Speech가 준비되지 않은 상태에서는 `SpeechAnalyzer` 내부 크래시 대신 명시적 오류를 반환하도록 보강

## 1.1.7

- Apple Speech는 Whisper용 16k 전처리 파일 대신 추출한 원본 PCM 오디오를 직접 사용하도록 변경
- Apple Speech probe에서 AppKit 초기화를 제거해 helper 자체 크래시를 방지
- 빌드 완료 후 `.app` 번들을 ad-hoc codesign 하도록 보강

## 1.1.6

- Apple Speech 실행을 별도 Python 자식 프로세스 대신 메인 앱 프로세스에서 직접 관리
- Apple Speech helper에 AppKit 초기화와 더 단순한 preset 구성을 적용
- Apple Speech 경로에서는 STT 용어 사전 컨텍스트를 전달하지 않도록 조정

## 1.1.5

- Apple Speech가 프레임워크 내부 오류로 실패하면 Whisper로 자동 fallback

## 1.1.4

- Apple Speech 실행 전 필요한 로컬 에셋 설치를 먼저 확인하도록 보강
- Apple Speech locale 입력값을 `ko` -> `ko-KR`처럼 정규화

## 1.1.3

- SpeechTranscriber 실행 중 충돌 시 DictationTranscriber로 자동 재시도
- SpeechTranscriber에는 STT 용어 사전 컨텍스트를 전달하지 않도록 조정

## 1.1.2

- 앱 번들 실행 시 Apple Speech 권한 helper 경로를 잘못 찾던 문제 수정

## 1.1.1

- Apple Speech 선택 및 실행 시 음성 인식 권한 요청을 자동화
- 권한 미승인 상태에서 `notDetermined`로 STT가 실패하던 흐름을 안내 메시지로 보완

## 1.1.0

- Apple Speech 기반 로컬 STT 백엔드 추가
- 메뉴에서 Whisper, Qwen3-ASR, Apple Speech 모델 전환 지원
- 앱 메뉴에 릴리즈 노트 항목 추가
- 버전 파일 기반으로 앱 번들 버전과 변경 이력 관리 시작

## 1.0.0

- 초기 메뉴바 앱 공개
- Whisper(MLX), Qwen3-ASR 기반 로컬 STT 지원
- 녹화/녹음, 음성 전처리, 회의록 생성 파이프라인 제공
