import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event, Timer
from typing import Callable, Optional

from cancellation import OperationCancelledError

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "opus"

# claude CLI 폴백 탐색 경로 (PATH에 없을 때 흔히 설치되는 위치들)
_CLAUDE_CLI_FALLBACK_PATHS = [
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".claude" / "local" / "claude"),
    str(Path.home() / ".local" / "bin" / "claude"),
]

# claude CLI 무인 호출 1회당 최대 대기 시간(초). 초과 시 프로세스를 강제 종료하고 재시도 대상으로 취급한다.
CLI_TIMEOUT_SECONDS = 600

SYSTEM_PROMPT = (
    "당신은 회의록 작성 전문가입니다. 주어진 회의 대본을 분석하여 구조화된 회의록을 작성하세요.\n"
    "반드시 한국어로 작성하세요.\n"
    "대본에는 `[HH:MM:SS] 텍스트` 형식의 타임라인이 포함되어 있습니다. 아젠다/결정사항/다음 액션/기타 메모 "
    "섹션의 각 항목 바로 아래에는 출처 하위 불릿을 추가하세요: 아젠다는 "
    "`   - 출처: [HH:MM:SS] \"인용된 대본 발언 내용\"`처럼 스페이스 3칸으로 들여쓰고, "
    "결정사항/다음 액션/기타 메모는 `\t- 출처: [HH:MM:SS] \"인용된 대본 발언 내용\"`처럼 "
    "탭(tab) 문자로 들여쓰세요. "
    "아젠다는 한 항목(출처 하위 불릿까지 포함)을 다 쓴 뒤 빈 줄을 하나 넣고 다음 번호 항목을 시작하세요. "
    "여러 시점에 걸친 내용은 쉼표로 나열하지 말고, 출처 하위 불릿을 한 줄에 하나씩 여러 개 열거하세요. "
    "출처는 반드시 대본에 실제로 존재하는 타임스탬프와 발언만 인용해야 하며, 절대 지어내지(환각) 마세요. "
    "대본에 [HH:MM:SS] 형식의 타임스탬프가 없다면 출처 하위 불릿 자체를 생략하세요."
)

USER_PROMPT_TEMPLATE = """아래 회의 대본을 분석하여 회의록을 작성해주세요.

## 작성 형식

# (회의 핵심 내용을 담은 제목을 10자 이내로 작성)

- 파일명: {filename}
- 일시: {datetime}

## 회의 요약
(2~3문장으로 전체 회의 내용 요약)

## 아젠다
1. (논의된 주제들을 순서대로 정리)
   - 출처: [HH:MM:SS] "인용된 대본 발언 내용"
   - 출처: [HH:MM:SS] "인용된 대본 발언 내용"

2. (다음 논의 주제)
   - 출처: [HH:MM:SS] "인용된 대본 발언 내용"

## 결정사항
- (회의에서 확정된 사항들)
	- 출처: [HH:MM:SS] "인용된 대본 발언 내용"

## 다음 액션
- [ ] 담당자 - 내용 - 기한
	- 출처: [HH:MM:SS] "인용된 대본 발언 내용"

## 기타 메모
- (분류하기 어려운 중요 발언이나 참고사항)
	- 출처: [HH:MM:SS] "인용된 대본 발언 내용"

## 출처 표기 규칙
- 대본은 `[HH:MM:SS] 텍스트` 형식의 타임라인을 포함합니다. 아젠다 항목 바로 아래에는 스페이스 3칸으로 들여쓴 출처 하위 불릿을, 결정사항/다음 액션/기타 메모 항목 바로 아래에는 탭(tab) 문자로 들여쓴 출처 하위 불릿을 `- 출처: [HH:MM:SS] "인용된 대본 발언 내용"` 형식으로 추가하세요.
- 아젠다는 한 항목의 출처 하위 불릿까지 모두 작성한 뒤 빈 줄을 하나 넣고 다음 번호 항목을 시작하세요(항목과 항목 사이는 빈 줄로 구분).
- 하나의 항목이 여러 시점에 걸쳐 언급되었다면 쉼표로 나열하지 말고, 출처 하위 불릿을 한 줄에 하나씩 여러 개 열거하세요.
- 인용문("...")은 해당 타임스탬프 라인의 실제 발언 원문 또는 그 요약이어야 합니다.
- 출처는 대본에 실제로 존재하는 타임스탬프와 발언만 인용해야 하며, 절대 지어내지 마세요(환각 금지).
- 대본에 [HH:MM:SS] 형식의 타임스탬프가 없다면 출처 하위 불릿 자체를 생략하세요.

---

## 대본:
{script}"""

MAX_RETRIES = 3
BASE_DELAY = 2

_INVALID_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


class _ClaudeCliError(Exception):
    """claude CLI 호출 실패. generate_note의 재시도 루프에서만 사용하는 내부 예외.

    retryable=False 이면 재시도해도 결과가 달라지지 않는 실패(인증 만료 등)라
    재시도 루프가 즉시 중단한다.
    """

    def __init__(self, message: str, retryable: bool = True, guidance: Optional[str] = None):
        super().__init__(message)
        self.retryable = retryable
        self.guidance = guidance


# CLI가 stdout JSON 의 error 필드로 알려주는, 재시도가 무의미한 실패들
_FATAL_CLI_ERRORS = {
    "authentication_failed": (
        "claude CLI 로그인이 만료되었습니다. 터미널에서 `claude` 를 실행한 뒤 "
        "`/login` 으로 다시 로그인하고 회의록 생성을 재시도하세요."
    ),
}


def find_claude_cli() -> Optional[str]:
    """claude CLI 실행 파일의 절대 경로를 찾는다.

    PATH에서 우선 탐색(`which claude`)하고, 없으면 흔히 설치되는 위치들을
    폴백으로 확인한다. 어디에서도 찾지 못하면 None을 반환한다.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _CLAUDE_CLI_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _extract_title(content: str) -> str:
    """생성된 회의록 첫 번째 H1에서 제목 추출. 파일명에 사용 가능한 형태로 반환."""
    m = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    if m:
        title = _INVALID_CHARS.sub('', m.group(1)).strip()
        return title[:50] if title else "회의록"
    return "회의록"


def _run_claude_cli(
    claude_path: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    estimated_chars: int,
    progress_callback: Optional[Callable[[str], None]],
    stop_event: Optional[Event],
) -> str:
    """claude CLI를 1회 실행하고 결과 본문을 반환한다.

    실패(비0 종료, 빈 결과, is_error==true, 타임아웃)는 _ClaudeCliError로 알려
    상위 재시도 루프가 처리하도록 하고, stop_event가 설정되면 프로세스를 종료한
    뒤 OperationCancelledError를 올린다.
    """
    argv = [
        claude_path,
        "--print",
        "--model", model,
        "--system-prompt", system_prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--allowedTools", "",
    ]

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except OSError as e:
        raise _ClaudeCliError(f"claude CLI 실행 실패: {e}") from e

    timed_out = Event()

    def _on_timeout():
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    timer = Timer(CLI_TIMEOUT_SECONDS, _on_timeout)
    timer.daemon = True
    timer.start()

    def _terminate_process():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        try:
            if proc.stdin is not None:
                proc.stdin.write(user_prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError) as e:
            _terminate_process()
            raise _ClaudeCliError(f"claude CLI stdin 전달 실패: {e}") from e

        result_text = None
        is_error = False
        error_code = None
        received_chars = 0

        for line in proc.stdout:
            if stop_event is not None and stop_event.is_set():
                _terminate_process()
                raise OperationCancelledError("회의록 생성이 중단되었습니다.")

            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # CLI 는 인증 만료 같은 실패를 stderr 가 아니라 stdout JSON 의 error 필드로 알린다.
            # 종료 코드만 보고 stderr 를 읽으면 빈 메시지가 되므로 여기서 코드를 잡아 둔다.
            if isinstance(event.get("error"), str):
                error_code = event["error"]

            event_type = event.get("type")
            if event_type == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    text = delta.get("text") or delta.get("partial_json") or ""
                    if text:
                        received_chars += len(text)
                        if progress_callback:
                            pct = min(received_chars / estimated_chars * 100, 99)
                            progress_callback(f"[5/6] 회의록 생성 중... {pct:.0f}%")
            elif event_type == "result":
                result_text = event.get("result")
                is_error = bool(event.get("is_error"))

        if stop_event is not None and stop_event.is_set():
            _terminate_process()
            raise OperationCancelledError("회의록 생성이 중단되었습니다.")

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out.set()
            _terminate_process()
    finally:
        timer.cancel()

    if timed_out.is_set():
        raise _ClaudeCliError(f"claude CLI 응답 시간 초과 ({CLI_TIMEOUT_SECONDS}초)")

    if proc.returncode != 0 or is_error:
        stderr_output = ""
        try:
            if proc.stderr is not None:
                stderr_output = proc.stderr.read()
        except Exception:
            pass

        # 원인은 대개 stdout JSON 쪽에 있다(stderr 는 비어 있는 경우가 많다).
        # 셋 중 실제로 값이 있는 것만 이어 붙여 빈 메시지가 나오지 않게 한다.
        details = [
            part.strip()
            for part in (error_code, result_text, stderr_output)
            if isinstance(part, str) and part.strip()
        ]
        reason = " | ".join(dict.fromkeys(details)) or "원인 정보 없음"

        guidance = _FATAL_CLI_ERRORS.get(error_code or "")
        message = f"claude CLI 실패 (종료 코드 {proc.returncode}): {reason}"
        if guidance:
            message = f"{message}\n{guidance}"
        raise _ClaudeCliError(message, retryable=guidance is None, guidance=guidance)

    if not result_text:
        raise _ClaudeCliError("claude CLI가 빈 결과를 반환했습니다.")

    if progress_callback:
        progress_callback("[5/6] 회의록 생성 완료 (100%)")

    return result_text


def generate_note(
    script_path: str,
    output_path: str,
    original_filename: str,
    created_at: str,
    model: str = DEFAULT_CLAUDE_MODEL,
    progress_callback: Optional[Callable[[str], None]] = None,
    stop_event: Optional[Event] = None,
):
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"대본 파일을 찾을 수 없습니다: {script_path}")

    claude_path = find_claude_cli()
    if not claude_path:
        raise RuntimeError(
            "claude CLI를 찾을 수 없습니다. https://claude.com/claude-code 안내에 따라 "
            "Claude Code CLI를 설치한 뒤 다시 시도하세요. 설치 후 터미널에서 "
            "`claude --version` 명령으로 설치 여부를 확인할 수 있습니다."
        )

    script_content = script.read_text(encoding="utf-8")
    estimated_chars = max(800, int(len(script_content) * 0.3))

    user_prompt = USER_PROMPT_TEMPLATE.format(
        filename=original_filename,
        datetime=created_at,
        script=script_content,
    )

    def _check_stop():
        if stop_event is not None and stop_event.is_set():
            raise OperationCancelledError("회의록 생성이 중단되었습니다.")

    last_error = None
    content = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _check_stop()
            logger.info("회의록 생성 CLI 호출 (시도 %d/%d, 모델: %s)", attempt, MAX_RETRIES, model)

            content = _run_claude_cli(
                claude_path=claude_path,
                model=model,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                estimated_chars=estimated_chars,
                progress_callback=progress_callback,
                stop_event=stop_event,
            )
            break

        except OperationCancelledError:
            raise
        except _ClaudeCliError as e:
            last_error = e
            if not e.retryable:
                # 인증 만료처럼 재시도해도 결과가 같은 실패 — 즉시 중단하고 조치 안내를 그대로 올린다
                logger.error("claude CLI 호출 실패 (재시도 불가): %s", e)
                raise RuntimeError(str(e)) from e
            if attempt < MAX_RETRIES:
                _check_stop()
                delay = BASE_DELAY ** attempt
                logger.warning("claude CLI 호출 실패 (시도 %d/%d), %d초 후 재시도: %s", attempt, MAX_RETRIES, delay, e)
                time.sleep(delay)
            else:
                logger.error("claude CLI 호출 최종 실패: %s", e)
                raise RuntimeError(f"claude CLI 호출 {MAX_RETRIES}회 실패: {last_error}") from e

    _check_stop()
    title = _extract_title(content)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")

    logger.info("회의록 생성 완료 → %s (제목: %s)", output.name, title)
    return str(output), title
