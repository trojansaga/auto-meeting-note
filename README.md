# AutoMeetingNote

macOS 메뉴바 앱으로, 화면 녹화 또는 오디오 녹음 후 자동으로 음성 전처리 → STT → 회의록 생성까지 수행합니다.

## 주요 기능

- **화면 녹화** — screencapture + ScreenCaptureKit으로 화면과 시스템 오디오를 동시 캡처
- **오디오 녹음** — ScreenCaptureKit으로 시스템 오디오만 캡처
- **마이크 녹음** — ffmpeg avfoundation으로 마이크 오디오 동시 녹음 (옵션)
- **일시 정지/재개** — 세그먼트 분할 방식으로 녹화·녹음 중 일시 정지
- **음성 전처리** — 노이즈 제거, 침묵 구간 제거, 음량 정규화
- **로컬 STT** — mlx-whisper (Apple Silicon 최적화) 로컬 음성 인식
- **회의록 생성** — OpenAI GPT API로 구조화된 회의록 자동 생성
- **수동 처리** — 기존 mp4/mov 파일을 선택하여 처리

## 요구사항

- macOS (Apple Silicon)
- Python 3.11+
- ffmpeg (`brew install ffmpeg`)
- OpenAI API 키

## 설치

```bash
# 환경 셋업
bash setup_env.sh

# 가상환경 활성화
source .venv/bin/activate

# .env 파일에 API 키 설정
echo "OPENAI_API_KEY=sk-..." > .env
```

## 빌드 및 실행

```bash
# .app 번들 빌드
bash build_app.sh

# 실행
open dist/AutoMeetingNote.app

# Applications에 설치
cp -R dist/AutoMeetingNote.app /Applications/
```

## 설정

설정 파일 위치: `~/Library/Application Support/AutoMeetingNote/config.yaml`

```yaml
watch_dir: "~/Desktop"           # 녹화/녹음 파일 저장 및 작업 디렉토리
whisper_model: "small"           # STT 모델 (small, medium, large-v3 등)
whisper_quant: "4bit"            # 모델 양자화 (4bit, 8bit, base)
whisper_batch_size: 4            # STT 배치 크기
language: "ko"                   # STT 언어
openai_model: "gpt-5.4"         # 회의록 생성 모델
export_dir: "~/Downloads"        # 회의록 내보내기 디렉토리
mic_enabled: false               # 마이크 동시 녹음 여부
mic_device_index: "0"            # 마이크 장치 인덱스
```

## 처리 파이프라인

```
녹화/녹음 완료 → 확인 팝업 → [1/6] 폴더 생성 → [2/6] 음성 추출
→ [3/6] 음성 전처리 → [4/6] STT → [5/6] 회의록 생성 → [6/6] 완료
```

## 프로젝트 구조

```
app.py                 # 메뉴바 앱 진입점
recorder.py            # 화면 녹화 / 오디오 녹음
system_audio.py        # ScreenCaptureKit 시스템 오디오 캡처
pipeline.py            # 파이프라인 오케스트레이션
audio_extractor.py     # mp4 → wav 추출
audio_preprocessor.py  # 음성 전처리
transcriber.py         # mlx-whisper STT
note_generator.py      # OpenAI API 회의록 생성
generate_prompt.py     # 회의록 생성 프롬프트 빌더
config.yaml            # 기본 설정
dictionary.txt         # STT 용어 사전
build_app.sh           # .app 번들 빌드 스크립트
setup_env.sh           # 환경 셋업 스크립트
```
