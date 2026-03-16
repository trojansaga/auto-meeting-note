# 회의록 자동 작성 서비스 - 바이브코딩 프롬프트

## 프로젝트 개요

macOS 메뉴바 앱으로, 특정 폴더를 감시하여 회의 녹화 mp4 파일이 감지되면 자동으로 STT → 대본 작성 → 회의록 생성까지 수행하는 파이프라인을 구현한다.

## 기술 스택

- Python 3.11+
- rumps: macOS 메뉴바 앱
- watchdog: 파일시스템 감시
- ffmpeg (subprocess): mp4에서 음성 추출
- openai-whisper (large-v3, 로컬): 한국어 STT (키 불필요, 로컬 실행)
- openai (Python SDK): 회의록 생성 (GPT API)
- python-dotenv: .env 파일에서 API 키 로드
- pyyaml: 설정 관리
- py2app: macOS 앱 번들 빌드

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
├── app.py                 # 메뉴바 앱 진입점
├── config.yaml            # 사용자 설정
├── watcher.py             # 폴더 감시
├── pipeline.py            # 파이프라인 오케스트레이션
├── audio_extractor.py     # mp4 → wav 추출
├── transcriber.py         # Whisper STT
├── note_generator.py      # Claude API 회의록 생성
├── requirements.txt
└── setup.py               # py2app 빌드
```

## 상세 요구사항

### 1. config.yaml

아래 항목을 포함하는 설정 파일을 생성한다.

```yaml
watch_dir: "~/Desktop"
file_prefix: "회의_"
whisper_model: "large-v3"
language: "ko"
openai_model: "gpt-5.3"
```

- API 키는 config.yaml에 저장하지 않고, 프로젝트 루트의 `.env` 파일에서 관리한다.
- `watch_dir`의 `~`는 런타임에 `os.path.expanduser()`로 확장한다.

`.env` 파일 형식:

```
OPENAI_API_KEY=sk-...
```

앱 시작 시 `python-dotenv`의 `load_dotenv()`를 호출하여 환경변수로 로드한다.

### 2. app.py - 메뉴바 앱

rumps 라이브러리로 macOS 메뉴바 앱을 구현한다.

- 메뉴바 아이콘: 텍스트 기반 ("📝" 또는 "MN")
- 메뉴 항목:
  - **감시 시작/중지** (토글): watcher 스레드를 시작/중지
  - **감시 폴더 열기**: Finder에서 watch_dir 열기
  - **설정 파일 열기**: config.yaml을 기본 에디터로 열기
  - **종료**
- 상태 표시: 감시 중일 때와 파이프라인 처리 중일 때 메뉴바 텍스트로 상태를 표시한다.
- 파이프라인 완료 시 macOS 알림(rumps.notification)을 발송한다.
- watcher는 별도 스레드에서 실행하여 메뉴바 앱이 블로킹되지 않도록 한다.

### 3. watcher.py - 폴더 감시

watchdog 라이브러리로 특정 폴더를 감시한다.

- `FileSystemEventHandler`를 상속하여 구현한다.
- `on_created` 또는 `on_moved` 이벤트에서 `.mp4` 확장자 + `file_prefix` 패턴에 매칭되는 파일만 처리한다.
- **파일 쓰기 완료 감지**: 녹화 중인 파일을 잡지 않도록, 파일 크기를 3초 간격으로 2회 비교하여 변동이 없을 때만 파이프라인을 시작한다.
- 동일 파일에 대해 중복 처리를 방지한다 (이미 처리된 파일 목록 관리).
- 파이프라인 호출은 별도 스레드에서 실행하여 감시가 중단되지 않도록 한다.

### 4. pipeline.py - 파이프라인 오케스트레이션

전체 처리 흐름을 순차적으로 실행한다.

```
입력: mp4 파일 경로
처리 순서:
  1. 파일명(확장자 제외)과 동일한 이름의 폴더를 watch_dir 내에 생성
  2. mp4 파일을 생성된 폴더로 이동
  3. audio_extractor 호출 → 폴더 내 audio.wav 생성
  4. transcriber 호출 → 폴더 내 script.md 생성
  5. note_generator 호출 → 폴더 내 meeting_note.md 생성
  6. 임시 audio.wav 파일 삭제 (디스크 절약)
  7. 상태 콜백을 통해 app.py에 완료 알림 전달
```

- 각 단계에서 예외 발생 시 해당 폴더에 `error.log`를 생성하고, 이후 단계를 중단한다.
- 각 단계 시작/완료 시 콜백 함수를 호출하여 메뉴바 상태를 업데이트한다.

### 5. audio_extractor.py - 음성 추출

ffmpeg를 subprocess로 호출하여 mp4에서 음성을 추출한다.

```python
# ffmpeg 명령어
ffmpeg -i input.mp4 -vn -acodec pcm_s16le -ar 16000 -ac 1 output.wav
```

- 16kHz, mono, PCM 16bit WAV로 추출 (Whisper 최적 입력 포맷)
- ffmpeg 미설치 시 명확한 에러 메시지를 출력한다.
- subprocess 실행 시 stdout/stderr를 캡처하여 에러 발생 시 로깅한다.

### 6. transcriber.py - STT (Whisper)

openai-whisper 라이브러리를 사용하여 로컬에서 STT를 수행한다.

- 모델: config의 `whisper_model` 값 사용 (기본 `large-v3`)
- 언어: `language="ko"` 고정
- 모델은 최초 실행 시 자동 다운로드되며, 이후 캐시에서 로드한다.
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

### 7. note_generator.py - 회의록 생성

OpenAI Python SDK를 사용하여 GPT API로 회의록을 생성한다.

- API 키는 `.env`에서 `load_dotenv()`로 로드한 뒤 `os.environ["OPENAI_API_KEY"]`로 읽는다.
- 사용 모델: config의 `openai_model` 값 (기본 `gpt-4o`)
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

### 8. requirements.txt

가상환경에서 `pip install -r requirements.txt`로 설치한다.

```
rumps>=0.4.0
watchdog>=3.0.0
openai-whisper>=20231117
openai>=1.0.0
python-dotenv>=1.0.0
pyyaml>=6.0
torch>=2.0.0
py2app>=0.28.0
```

### 9. .gitignore

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

### 10. setup.py (py2app 빌드)

py2app을 사용하여 macOS .app 번들로 빌드할 수 있도록 setup.py를 작성한다. 반드시 가상환경 활성화 상태에서 `python setup.py py2app`으로 빌드한다.

```python
from setuptools import setup

APP = ['app.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'LSUIElement': True,  # Dock에 표시하지 않음 (메뉴바 전용)
    },
    'packages': ['rumps', 'watchdog', 'whisper', 'openai', 'dotenv', 'torch'],
}

setup(
    app=APP,
    name='AutoMeetingNote',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
```

### 11. 처리 현황 상세 표시 (예정)

파이프라인 각 단계에서 더 구체적인 진행 상황을 메뉴바에 표시한다.

#### 단계별 상세 진행 정보

| 단계 | 현재 | 목표 |
|------|------|------|
| 음성 추출 | `[2/5] 음성 추출` | `[2/5] 음성 추출 중... (00:01:23 / 00:45:00)` — ffmpeg stderr에서 time= 파싱 |
| STT | `[3/5] STT 처리` | `[3/5] STT 처리 중... 32%` — Whisper `segments` 진행률 콜백 |
| 회의록 생성 | `[4/5] 회의록 생성` | `[4/5] 회의록 생성 중... (스트리밍)` — OpenAI streaming으로 토큰 수신 표시 |

#### 구현 방식

- **음성 추출**: `subprocess.Popen`으로 ffmpeg를 실행하고, stderr에서 `time=HH:MM:SS` 패턴을 실시간 파싱하여 진행률 콜백 호출
- **STT**: Whisper의 `transcribe()` 결과 segments를 처리하기 전 전체 오디오 길이 대비 현재 segment 시작 시각으로 진행률(%) 계산. Whisper 내부 훅 또는 segment 단위 순회로 구현
- **회의록 생성**: OpenAI SDK의 streaming 모드(`stream=True`)로 호출하여 청크 수신 시마다 콜백

#### 영향 파일

- `audio_extractor.py`: `subprocess.Popen` + stderr 실시간 읽기, `progress_callback` 파라미터 추가
- `transcriber.py`: segment 순회 중 진행률 계산, `progress_callback` 파라미터 추가
- `note_generator.py`: streaming 모드 전환, `progress_callback` 파라미터 추가
- `pipeline.py`: 각 모듈에 `progress_callback` 전달, 상태 메시지 포맷 변경
- `app.py`: 기존 `status_callback` 그대로 활용 (변경 불필요)

## 구현 주의사항

1. **스레딩**: rumps 메인 루프와 watcher/pipeline은 반드시 별도 스레드에서 실행한다. rumps의 메인 루프를 블로킹하면 앱이 응답 불가 상태가 된다.
2. **파일 경로**: 모든 경로에서 한글 파일명을 정상 처리해야 한다. `os.path`를 사용하여 경로를 조합한다.
3. **Whisper 모델 로딩**: 모델 로딩에 시간이 걸리므로, 앱 시작 시 또는 최초 처리 시 한 번만 로드하고 재사용한다.
4. **디스크 용량**: audio.wav는 처리 완료 후 삭제하여 디스크 공간을 절약한다. 1시간 회의의 WAV 파일은 약 115MB이다.
5. **로깅**: Python logging 모듈을 사용하여 각 모듈에서 처리 상태를 로깅한다. 로그 파일은 `~/Library/Logs/AutoMeetingNote/app.log`에 저장한다.
6. **동시 처리 방지**: 하나의 파이프라인이 실행 중일 때 새 파일이 감지되면 큐에 넣고 순차 처리한다 (Whisper가 GPU 메모리를 점유하므로 동시 실행 방지).
