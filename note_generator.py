import logging
import re
import time
from pathlib import Path
from threading import Event
from typing import Callable, Optional

import openai

from cancellation import OperationCancelledError

logger = logging.getLogger(__name__)

NOTE_PROVIDERS = {
    "openai": "OpenAI",
    "naver": "Naver AI Gateway",
}

NAVER_DEFAULT_BASE_URL = "https://namc-aigw.io.naver.com"

SYSTEM_PROMPT = (
    "당신은 회의록 작성 전문가입니다. 주어진 회의 대본을 분석하여 구조화된 회의록을 작성하세요.\n"
    "반드시 한국어로 작성하세요."
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

## 결정사항
- (회의에서 확정된 사항들)

## 다음 액션
- [ ] 담당자 - 내용 - 기한

## 기타 메모
- (분류하기 어려운 중요 발언이나 참고사항)

---

## 대본:
{script}"""

MAX_RETRIES = 3
BASE_DELAY = 2

_INVALID_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _create_client(
    provider: str,
    naver_api_key: str = "",
    naver_api_base_url: str = NAVER_DEFAULT_BASE_URL,
) -> openai.OpenAI:
    if provider == "naver":
        if not naver_api_key:
            raise RuntimeError("Naver API 키가 설정되지 않았습니다. config.yaml의 naver_api_key를 확인하세요.")
        return openai.OpenAI(
            base_url=naver_api_base_url.rstrip("/"),
            api_key=naver_api_key,
        )
    return openai.OpenAI()


def _extract_title(content: str) -> str:
    """생성된 회의록 첫 번째 H1에서 제목 추출. 파일명에 사용 가능한 형태로 반환."""
    m = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    if m:
        title = _INVALID_CHARS.sub('', m.group(1)).strip()
        return title[:50] if title else "회의록"
    return "회의록"


def generate_note(
    script_path: str,
    output_path: str,
    original_filename: str,
    created_at: str,
    model: str = "gpt-5.4",
    provider: str = "openai",
    naver_api_key: str = "",
    naver_api_base_url: str = NAVER_DEFAULT_BASE_URL,
    progress_callback: Optional[Callable[[str], None]] = None,
    stop_event: Optional[Event] = None,
) -> str:
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"대본 파일을 찾을 수 없습니다: {script_path}")

    script_content = script.read_text(encoding="utf-8")
    estimated_chars = max(800, int(len(script_content) * 0.3))

    user_prompt = USER_PROMPT_TEMPLATE.format(
        filename=original_filename,
        datetime=created_at,
        script=script_content,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    client = _create_client(provider, naver_api_key, naver_api_base_url)
    # temperature를 지원하지 않는 모델(Bedrock Claude 등)을 위해 provider별로 분리
    extra_kwargs = {"temperature": 0.3} if provider == "openai" else {}
    last_error = None

    def _check_stop():
        if stop_event is not None and stop_event.is_set():
            raise OperationCancelledError("회의록 생성이 중단되었습니다.")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _check_stop()
            logger.info("회의록 생성 API 호출 (시도 %d/%d, provider: %s, 모델: %s)", attempt, MAX_RETRIES, provider, model)

            if progress_callback or stop_event is not None:
                content_parts = []
                received_chars = 0

                with client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    **extra_kwargs,
                ) as stream:
                    for chunk in stream:
                        _check_stop()
                        delta = chunk.choices[0].delta.content or ""
                        content_parts.append(delta)
                        received_chars += len(delta)
                        if progress_callback:
                            pct = min(received_chars / estimated_chars * 100, 99)
                            progress_callback(f"[5/6] 회의록 생성 중... {pct:.0f}%")

                content = "".join(content_parts)
                if progress_callback:
                    progress_callback("[5/6] 회의록 생성 완료 (100%)")
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **extra_kwargs,
                )
                content = response.choices[0].message.content

            break

        except OperationCancelledError:
            raise
        except (openai.NotFoundError, openai.AuthenticationError, openai.BadRequestError) as e:
            logger.error("재시도 불가 오류: %s", e)
            raise RuntimeError(f"API 오류: {e}") from e
        except (openai.APIConnectionError, openai.RateLimitError, openai.APIError) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                _check_stop()
                delay = BASE_DELAY ** attempt
                logger.warning("API 호출 실패 (시도 %d/%d), %d초 후 재시도: %s", attempt, MAX_RETRIES, delay, e)
                time.sleep(delay)
            else:
                logger.error("API 호출 최종 실패: %s", e)
                raise RuntimeError(f"API 호출 {MAX_RETRIES}회 실패: {last_error}") from e

    _check_stop()
    title = _extract_title(content)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")

    logger.info("회의록 생성 완료 → %s (제목: %s)", output.name, title)
    return str(output), title
