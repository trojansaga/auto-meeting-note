# Release Notes

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
