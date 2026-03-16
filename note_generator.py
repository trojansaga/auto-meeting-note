import logging
import time
from pathlib import Path
from typing import Callable, Optional

import openai

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "당신은 회의록 작성 전문가입니다. 주어진 회의 대본을 분석하여 구조화된 회의록을 작성하세요.\n"
    "반드시 한국어로 작성하세요."
)

USER_PROMPT_TEMPLATE = """아래 회의 대본을 분석하여 회의록을 작성해주세요.

## 작성 형식

# 회의록

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


def generate_note(
    script_path: str,
    output_path: str,
    original_filename: str,
    created_at: str,
    model: str = "gpt-5.3",
    progress_callback: Optional[Callable[[str], None]] = None,
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

    client = openai.OpenAI()
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("회의록 생성 API 호출 (시도 %d/%d, 모델: %s)", attempt, MAX_RETRIES, model)

            if progress_callback:
                content_parts = []
                received_chars = 0

                with client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    stream=True,
                ) as stream:
                    for chunk in stream:
                        delta = chunk.choices[0].delta.content or ""
                        content_parts.append(delta)
                        received_chars += len(delta)
                        pct = min(received_chars / estimated_chars * 100, 99)
                        progress_callback(f"[4/5] 회의록 생성 중... {pct:.0f}%")

                content = "".join(content_parts)
                progress_callback("[4/5] 회의록 생성 완료 (100%)")
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                )
                content = response.choices[0].message.content

            break

        except (openai.APIError, openai.APIConnectionError, openai.RateLimitError) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY ** attempt
                logger.warning("API 호출 실패 (시도 %d/%d), %d초 후 재시도: %s", attempt, MAX_RETRIES, delay, e)
                time.sleep(delay)
            else:
                logger.error("API 호출 최종 실패: %s", e)
                raise RuntimeError(f"OpenAI API 호출 {MAX_RETRIES}회 실패: {last_error}") from e

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")

    logger.info("회의록 생성 완료 → %s", output.name)
    return str(output)
