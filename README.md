# AutoMeetingNote

macOS 메뉴바 앱으로, 화면 녹화 또는 오디오 녹음 후 자동으로 음성 전처리 → STT → 회의록 생성까지 수행합니다.

## 주요 기능

- **화면 녹화** — screencapture + ScreenCaptureKit으로 화면과 시스템 오디오를 동시 캡처
- **오디오 녹음** — ScreenCaptureKit으로 시스템 오디오만 캡처
- **마이크 녹음** — ffmpeg avfoundation으로 마이크 오디오 동시 녹음 (옵션)
- **일시 정지/재개** — 세그먼트 분할 방식으로 녹화·녹음 중 일시 정지
- **음성 전처리** — 노이즈 제거, 침묵 구간 제거, 음량 정규화
- **로컬 STT** — Whisper(mlx-whisper), Qwen3-ASR, Apple Speech 중 선택 가능한 로컬 음성 인식
- **회의록 생성** — OpenAI GPT API로 구조화된 회의록 자동 생성
- **수동 처리** — 기존 mp4/mov 파일을 선택하여 처리
- **릴리즈 노트** — 메뉴에서 현재 버전과 변경 이력 확인

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
stt_backend: "whisper"           # STT 백엔드 (whisper, qwen3_asr, apple_speech)
whisper_model: "small"           # STT 모델 (small, medium, large-v3 등)
whisper_quant: "4bit"            # 모델 양자화 (4bit, 8bit, base)
whisper_batch_size: 4            # STT 배치 크기
qwen_model: "Qwen/Qwen3-ASR-0.6B" # Qwen3-ASR 모델 ID 또는 로컬 경로
apple_speech_model: "speech_transcriber" # Apple Speech 모델 (speech_transcriber, dictation_transcriber)
qwen_dtype: null                 # 예: float16, bfloat16, float32
qwen_device_map: null            # 예: auto, cpu, cuda:0, mps
qwen_attn_implementation: null   # null이면 MPS에서 eager attention 자동 사용
qwen_forced_aligner: null        # 타임스탬프용 aligner 모델 ID 또는 로컬 경로
qwen_return_timestamps: false    # true면 forced aligner 사용 시 타임스탬프 포함
qwen_max_new_tokens: 4096        # Qwen3-ASR 최대 생성 토큰 수
qwen_max_batch_size: 1           # Qwen3-ASR 추론 배치 제한
qwen_chunk_seconds: 600          # 긴 오디오를 Qwen 입력 청크로 나누는 길이(초)
language: "ko"                   # STT 언어
openai_model: "gpt-5.4"         # 회의록 생성 모델
export_dir: "~/Downloads"        # 회의록 내보내기 디렉토리
mic_enabled: false               # 마이크 동시 녹음 여부
mic_device_index: "macbook"      # macbook 또는 iphone
```

메뉴바의 `녹화/녹음 옵션 > 마이크 입력`에서 맥북/현재 디바이스와 iPhone 마이크를 선택할 수 있습니다.  
메뉴바의 `STT 모델` 메뉴에서 `Whisper (MLX)`, `Qwen3-ASR`, `Apple Speech`를 전환할 수 있습니다.  
Qwen3-ASR는 `qwen-asr` 패키지가 필요하며, `qwen_model`에는 Hugging Face 모델 ID 대신 로컬 디렉토리 경로도 지정할 수 있습니다.  
Apple Speech는 macOS 26 이상과 Speech 권한이 필요하며, `speech_transcriber`와 `dictation_transcriber` 중 선택할 수 있습니다.  
기본값에서는 Mac MPS에서 `eager` attention과 `600초` 청크를 사용하고, MPS 실패 시 `float32`와 CPU로 단계적으로 재시도합니다. Whisper 경로는 이 설정의 영향을 받지 않습니다.

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
transcriber.py         # Whisper / Qwen3-ASR / Apple Speech STT
apple_speech_probe.swift # Apple Speech 권한/지원 상태 점검 helper
apple_speech_transcriber.swift # Apple Speech 파일 전사용 helper
note_generator.py      # OpenAI API 회의록 생성
generate_prompt.py     # 회의록 생성 프롬프트 빌더
config.yaml            # 기본 설정
dictionary.txt         # STT 용어 사전
RELEASE_NOTES.md       # 버전별 변경 이력
VERSION                # 현재 앱 버전
build_app.sh           # .app 번들 빌드 스크립트
setup_env.sh           # 환경 셋업 스크립트
```
