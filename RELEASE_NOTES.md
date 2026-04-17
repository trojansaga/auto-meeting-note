# Release Notes

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
