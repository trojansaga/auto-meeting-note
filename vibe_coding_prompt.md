# 회의록 자동 작성 서비스 - 바이브코딩 프롬프트

## 프로젝트 개요

macOS 메뉴바 앱으로, 화면 녹화 또는 오디오 녹음을 직접 수행하고 완료 후 확인 팝업을 거쳐 자동으로 음성 전처리 → STT → 회의록 생성까지 수행하는 파이프라인을 구현한다. 외부 파일 선택을 통한 수동 처리도 지원한다.

## 기술 스택

- Python 3.11+
- rumps: macOS 메뉴바 앱
- ScreenCaptureKit (pyobjc): macOS 시스템 오디오 캡처
- screencapture (macOS 내장): 화면 녹화
- ffmpeg (subprocess): 음성 추출, 영상 압축, 오디오 믹싱
- mlx-whisper / lightning-whisper-mlx: Apple Silicon 최적화 로컬 STT
- noisereduce / soundfile: 음성 전처리 (노이즈 제거, VAD, 음량 정규화)
- openai (Python SDK): 회의록 생성 (GPT API)
- python-dotenv: .env 파일에서 API 키 로드
- pyyaml: 설정 관리
- Swift 런처 + build_app.sh: macOS .app 번들 빌드

시스템 의존성: `brew install ffmpeg`

## 환경 구성

프로젝트 루트에 Python 가상환경을 구성하고, 모든 패키지는 가상환경 내에서 설치·실행한다.

### 초기 환경 셋업 스크립트 (setup_env.sh)

프로젝트 루트에 아래 내용의 `setup_env.sh`를 생성한다. 실행 시 가상환경 생성부터 패키지 설치까지 한 번에 수행된다.

```bash
#!/bin/bash
set -e

VENV_DIR=".venv"

# 가상환경 생성
if [ ! -d "$VENV_DIR" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv "$VENV_DIR"
fi

# 가상환경 활성화
source "$VENV_DIR/bin/activate"

# pip 업그레이드
pip install --upgrade pip

# 패키지 설치
pip install -r requirements.txt

echo ""
echo "환경 구성 완료. 아래 명령어로 가상환경을 활성화하세요:"
echo "  source $VENV_DIR/bin/activate"
```

### 규칙

- 가상환경 디렉토리명: `.venv`
- 가상환경은 `python3 -m venv .venv`로 생성한다.
- 모든 pip install은 가상환경 활성화 상태에서 수행한다.
- `.venv/` 디렉토리는 `.gitignore`에 추가한다.
- app.py 실행 시에도 반드시 가상환경의 Python 인터프리터를 사용한다.
- py2app 빌드 시에도 가상환경 내에서 실행한다.
- API 키는 프로젝트 루트의 `.env` 파일에 저장하며, `python-dotenv`로 로드한다. `.env`는 `.gitignore`에 반드시 추가한다.

## 프로젝트 구조

```
auto-meeting-note/
├── .venv/                 # Python 가상환경 (git 제외)
├── .gitignore
├── setup_env.sh           # 환경 셋업 스크립트
├── build_app.sh           # .app 번들 빌드 스크립트 (Swift 런처 컴파일 포함)
├── app.py                 # 메뉴바 앱 진입점
├── config.yaml            # 사용자 설정
├── recorder.py            # 화면 녹화 / 오디오 녹음 (screencapture + SCStream)
├── system_audio.py        # ScreenCaptureKit 기반 시스템 오디오 캡처
├── pipeline.py            # 파이프라인 오케스트레이션
├── audio_extractor.py     # mp4 → wav 추출
├── audio_preprocessor.py  # 음성 전처리 (노이즈 제거, VAD, 음량 정규화)
├── transcriber.py         # mlx-whisper STT
├── note_generator.py      # OpenAI API 회의록 생성
├── generate_prompt.py     # 회의록 생성 프롬프트 빌더
├── dictionary.txt         # STT 용어 사전 (initial_prompt)
├── filter_prompt.txt      # 회의 내용 필터링 프롬프트
├── requirements.txt
└── setup.py               # py2app 설정 (참조용)
```

## 상세 요구사항

### 1. config.yaml

아래 항목을 포함하는 설정 파일을 생성한다.

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

- API 키는 config.yaml에 저장하지 않고, `~/Library/Application Support/AutoMeetingNote/.env` 파일에서 관리한다.
- `watch_dir`, `export_dir`의 `~`는 런타임에 `Path.expanduser()`로 확장한다.
- 설정 파일은 `~/Library/Application Support/AutoMeetingNote/config.yaml`에 저장되며, 없으면 번들 기본값에서 복사한다.

`.env` 파일 형식:

```
OPENAI_API_KEY=sk-...
```

앱 시작 시 `python-dotenv`의 `load_dotenv()`를 호출하여 환경변수로 로드한다.

### 2. app.py - 메뉴바 앱

rumps 라이브러리로 macOS 메뉴바 앱을 구현한다.

- 메뉴바 아이콘: 텍스트 기반 ("MN"), 녹화 중에는 "● REC MM:SS" 표시
- 메뉴 항목:
  - **파일 선택하여 처리...**: NSOpenPanel으로 mp4/mov 파일을 선택하여 수동 처리
  - **화면 녹화 시작/중지** (토글): screencapture + SCStream으로 화면 녹화
  - **녹음 시작/중지** (토글): SCStream으로 시스템 오디오 녹음
  - **일시 정지/재개**: 녹화·녹음 중 일시 정지
  - **녹화/녹음 옵션**: 마이크 녹음 포함, STT 건너뛰기 토글
  - **처리 현황**: 파이프라인 진행 상태 표시
  - **STT 모델 선택**: Whisper 모델/양자화 변경 및 다운로드
  - **전처리 설정**: 노이즈 제거, 침묵 구간 제거, 음량 정규화 토글
  - **설정 파일 열기** / **STT 용어 사전 열기** / **로그 파일 열기**
  - **종료**
- 녹화/녹음 완료 시 확인 팝업을 표시하여 회의록 생성 여부를 사용자가 결정한다.
- 파이프라인 완료 시 macOS 알림(rumps.notification)을 발송한다.
- 녹화·녹음·파이프라인은 별도 스레드에서 실행하여 메뉴바 앱이 블로킹되지 않도록 한다.
- UI 업데이트는 rumps.Timer를 통해 메인 스레드에서 flush한다.

### 3. recorder.py - 녹화/녹음

화면 녹화와 오디오 녹음을 담당한다.

- **화면 녹화**: macOS `screencapture -v` 명령을 subprocess로 실행, SCStream으로 시스템 오디오를 동시 캡처
- **오디오 녹음**: SCStream으로 시스템 오디오만 캡처 (화면 녹화 없음)
- **마이크 녹음**: ffmpeg의 avfoundation 입력을 사용하여 마이크 오디오 동시 녹음
- **일시 정지/재개**: 세그먼트 분할 방식으로 구현 (정지 시 현재 세그먼트 종료, 재개 시 새 세그먼트 시작)
- **후처리**: ffmpeg로 MOV→MP4 압축, 시스템 오디오·마이크 오디오 믹싱, 세그먼트 결합
- 녹화 파일은 `watch_dir`에 타임스탬프 기반 파일명으로 저장

### 4. system_audio.py - 시스템 오디오 캡처

ScreenCaptureKit (pyobjc) 기반으로 macOS 시스템 오디오를 캡처한다.

- SCStream API를 사용하여 시스템 전체 오디오를 PCM WAV로 캡처
- CoreMedia 프레임워크로 오디오 버퍼 데이터 직접 추출
- 16kHz, mono, 16bit PCM 포맷

### 5. pipeline.py - 파이프라인 오케스트레이션

전체 처리 흐름을 순차적으로 실행한다.

```
입력: mp4/mov/wav 파일 경로
처리 순서:
  1. [1/6] 파일명 기반 작업 폴더를 watch_dir 내에 생성, 파일 이동
  2. [2/6] 음성 추출 (mp4/mov → wav, WAV 직접 입력 시 건너뜀)
  3. [3/6] 음성 전처리 (노이즈 제거, 침묵 구간 제거, 음량 정규화)
  4. [4/6] STT 처리 (mlx-whisper)
  5. [5/6] 회의록 생성 (OpenAI GPT API)
  6. [6/6] 완료 → export_dir로 회의록 복사
```

- 기존 결과물이 있으면 confirm_callback으로 재처리 여부를 확인한다.
- 각 단계에서 예외 발생 시 해당 폴더에 `error.log`를 생성하고, 이후 단계를 중단한다.
- 각 단계 시작/완료 시 콜백 함수를 호출하여 메뉴바 상태를 업데이트한다.
- 완료된 회의록은 `export_dir` (기본: ~/Downloads)로 자동 복사한다.

### 6. audio_extractor.py - 음성 추출

ffmpeg를 subprocess로 호출하여 mp4에서 음성을 추출한다.

```python
# ffmpeg 명령어
ffmpeg -i input.mp4 -vn -acodec pcm_s16le -ar 16000 -ac 1 output.wav
```

- 16kHz, mono, PCM 16bit WAV로 추출 (Whisper 최적 입력 포맷)
- ffmpeg 미설치 시 명확한 에러 메시지를 출력한다.
- subprocess 실행 시 stdout/stderr를 캡처하여 에러 발생 시 로깅한다.

### 7. audio_preprocessor.py - 음성 전처리

STT 정확도 향상을 위해 음성 파일을 전처리한다.

- **노이즈 제거**: noisereduce 라이브러리 사용
- **침묵 구간 제거 (VAD)**: 에너지 기반 음성 활동 감지로 무음 구간 제거
- **음량 정규화**: 피크 기준 정규화
- 각 단계는 config에서 개별적으로 on/off 가능 (`preprocess_noise_reduce`, `preprocess_vad`, `preprocess_normalize`)

### 8. transcriber.py - STT (mlx-whisper)

mlx-whisper / lightning-whisper-mlx를 사용하여 Apple Silicon에서 로컬 STT를 수행한다.

- 모델: config의 `whisper_model` 값 사용 (기본 `small`)
- 양자화: config의 `whisper_quant` 값 사용 (기본 `4bit`)
- 언어: config의 `language` 값 사용 (기본 `ko`)
- 모델은 HuggingFace Hub에서 다운로드되며, `~/Library/Application Support/AutoMeetingNote/huggingface`에 캐시된다.
- `dictionary.txt`의 용어를 initial_prompt로 전달하여 도메인 특화 용어의 인식 정확도를 향상한다.
- 출력 형식 (script.md):

```markdown
# 회의 대본

- 파일명: {원본 mp4 파일명}
- 생성일시: {처리 시각}

## Transcript

[00:00:12] 안녕하세요, 오늘 회의를 시작하겠습니다.
[00:00:18] 첫 번째 안건은 프로젝트 일정 검토입니다.
...
```

- Whisper 결과의 segments에서 start 타임스탬프를 `[HH:MM:SS]` 형식으로 변환하여 각 세그먼트 앞에 붙인다.

### 9. note_generator.py - 회의록 생성

OpenAI Python SDK를 사용하여 GPT API로 회의록을 생성한다.

- API 키는 `.env`에서 `load_dotenv()`로 로드한 뒤 `os.environ["OPENAI_API_KEY"]`로 읽는다.
- 사용 모델: config의 `openai_model` 값 (기본 `gpt-5.4`)
- script.md의 내용을 입력으로 전달한다.
- 시스템 프롬프트:

```
당신은 회의록 작성 전문가입니다. 주어진 회의 대본을 분석하여 구조화된 회의록을 작성하세요.
반드시 한국어로 작성하세요.
```

- 유저 프롬프트:

```
아래 회의 대본을 분석하여 회의록을 작성해주세요.

## 작성 형식

# 회의록

- 파일명: {파일명}
- 일시: {생성일시}

## 회의 요약
(2~3문장으로 전체 회의 내용 요약)

## 아젠다
1. (논의된 주제들을 순서대로 정리)

## 결정사항
- (회의에서 확정된 사항들)

## 다음 액션
- [ ] 담당자 - 내용 - 기한

## 기타 메모
- (분류하기 어려운 중요 발언이나 참고사항)

---

## 대본:
{script.md 내용}
```

- 출력을 meeting_note.md로 저장한다.
- API 호출 실패 시 재시도 로직을 포함한다 (최대 3회, 지수 백오프).

### 10. requirements.txt

가상환경에서 `pip install -r requirements.txt`로 설치한다.

```
rumps>=0.4.0
noisereduce>=3.0.0
soundfile>=0.12.0
watchdog>=3.0.0
openai-whisper>=20231117
mlx-whisper>=0.4.3
lightning-whisper-mlx>=0.0.10
mlx>=0.31.0
openai>=1.0.0
python-dotenv>=1.0.0
pyyaml>=6.0
torch>=2.0.0
py2app>=0.28.0
setproctitle>=1.3.0
pyobjc-framework-Quartz>=9.0
pyobjc-framework-AVFoundation>=9.0
pyobjc-framework-ScreenCaptureKit>=12.0
```

### 11. .gitignore

```
.venv/
.env
__pycache__/
*.pyc
build/
dist/
*.egg-info/
.DS_Store
```

### 12. build_app.sh - 앱 빌드

`build_app.sh`로 macOS .app 번들을 빌드한다.

- Swift로 네이티브 런처 바이너리를 컴파일하여 `MacOS/` 폴더에 배치
- 런처는 자식 프로세스로 Python 앱을 실행하고, 시그널(SIGTERM, SIGINT, SIGHUP)을 전달
- 소스 파일과 config.yaml, dictionary.txt를 `Resources/`에 복사
- Info.plist에 `LSUIElement: true`를 설정하여 Dock에 표시하지 않음

```bash
bash build_app.sh
# → dist/AutoMeetingNote.app 생성
```

## 구현 주의사항

1. **스레딩**: rumps 메인 루프와 녹화/파이프라인은 반드시 별도 스레드에서 실행한다. rumps의 메인 루프를 블로킹하면 앱이 응답 불가 상태가 된다. UI 업데이트는 rumps.Timer를 통해 메인 스레드에서 flush한다.
2. **파일 경로**: 모든 경로에서 한글 파일명을 정상 처리해야 한다. `pathlib.Path`를 사용하여 경로를 조합한다.
3. **Whisper 모델**: mlx-whisper 모델은 HuggingFace Hub에서 다운로드한다. 앱 시작 시 모델 존재 여부를 확인하고, 없으면 다운로드 팝업을 표시한다.
4. **로깅**: Python logging 모듈을 사용하여 각 모듈에서 처리 상태를 로깅한다. 로그 파일은 `~/Library/Logs/AutoMeetingNote/app.log`에 append 모드로 저장한다.
5. **동시 처리 방지**: 하나의 파이프라인이 실행 중일 때 새 파일은 순차 처리한다.
6. **권한**: 화면 녹화/시스템 오디오 캡처에는 macOS의 화면 녹화 권한이 필요하다. 앱 시작 시 권한을 확인하고 없으면 안내 팝업을 표시한다.
7. **녹화 후 확인**: 녹화/녹음 완료 후 바로 파이프라인을 실행하지 않고, 확인 팝업을 표시하여 사용자가 회의록 생성 여부를 선택한다.
