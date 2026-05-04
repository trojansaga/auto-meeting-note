import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai

import note_generator
from cancellation import OperationCancelledError


def _make_chat_response(content: str):
    """비-스트리밍 client.chat.completions.create 응답 mock."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _make_stream_chunk(text: str):
    """스트리밍 chunk: chunk.choices[0].delta.content 접근만 사용."""
    delta = SimpleNamespace(content=text)
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(choices=[choice])


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._chunks)


class ExtractTitleTests(unittest.TestCase):
    def test_extract_title_strips_invalid_filename_chars(self):
        content = "# 회의: 결정/사항 *중요*?\n본문"
        title = note_generator._extract_title(content)
        # '/', ':', '*', '?' 등 파일명 금지 문자 제거
        for ch in "/\\:*?\"<>|":
            self.assertNotIn(ch, title)
        self.assertIn("회의", title)

    def test_extract_title_returns_default_when_no_h1(self):
        self.assertEqual(note_generator._extract_title("본문만 있음"), "회의록")

    def test_extract_title_clamps_to_50_chars(self):
        long = "# " + ("가" * 80)
        self.assertEqual(len(note_generator._extract_title(long)), 50)


class CreateClientTests(unittest.TestCase):
    def test_naver_provider_requires_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "Naver API 키"):
            note_generator._create_client("naver", naver_api_key="")

    def test_openai_provider_uses_default_constructor(self):
        with patch("note_generator.openai.OpenAI") as m_openai:
            note_generator._create_client("openai")
            m_openai.assert_called_once_with()

    def test_naver_provider_strips_trailing_slash_from_base_url(self):
        with patch("note_generator.openai.OpenAI") as m_openai:
            note_generator._create_client(
                "naver",
                naver_api_key="key-123",
                naver_api_base_url="https://example.io/",
            )
            m_openai.assert_called_once_with(base_url="https://example.io", api_key="key-123")


class GenerateNoteTests(unittest.TestCase):
    def _setup_script(self, tmp: str, content: str = "회의 대본 내용") -> tuple[Path, Path]:
        script = Path(tmp) / "script.md"
        script.write_text(content, encoding="utf-8")
        out = Path(tmp) / "out" / "note.md"
        return script, out

    def test_generate_note_raises_when_script_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no.md"
            out = Path(tmp) / "note.md"
            with self.assertRaisesRegex(FileNotFoundError, "대본 파일"):
                note_generator.generate_note(
                    str(missing),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

    def test_generate_note_writes_output_and_returns_title_in_streaming_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            chunks = [
                _make_stream_chunk("# 결정사항\n"),
                _make_stream_chunk("본문 1\n"),
                _make_stream_chunk("본문 2"),
            ]
            client = MagicMock()
            client.chat.completions.create.return_value = _FakeStream(chunks)

            statuses: list[str] = []
            with patch.object(note_generator, "_create_client", return_value=client):
                path_str, title = note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                    progress_callback=statuses.append,
                )

            self.assertEqual(path_str, str(out))
            self.assertEqual(title, "결정사항")
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("# 결정사항", content)
            self.assertIn("본문 2", content)
            # progress 가 stream / 완료 모두 보고됨
            joined = "\n".join(statuses)
            self.assertIn("회의록 생성 중", joined)
            self.assertIn("100%", joined)
            # streaming API 가 사용됨
            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertTrue(kwargs.get("stream"))

    def test_generate_note_uses_non_streaming_when_no_callback_or_stop_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            client = MagicMock()
            client.chat.completions.create.return_value = _make_chat_response("# 제목\n본문")

            with patch.object(note_generator, "_create_client", return_value=client):
                path_str, title = note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

            self.assertEqual(title, "제목")
            self.assertTrue(out.exists())
            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertNotIn("stream", kwargs)

    def test_generate_note_stop_event_raises_cancelled_during_streaming(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            stop_event = threading.Event()

            def _chunk_iter():
                yield _make_stream_chunk("# Title\n")
                stop_event.set()  # 두 번째 chunk 직전에 cancel 발화
                yield _make_stream_chunk("이건 처리되면 안 됨")

            client = MagicMock()
            client.chat.completions.create.return_value = _FakeStream(list(_chunk_iter()))

            with patch.object(note_generator, "_create_client", return_value=client):
                with self.assertRaises(OperationCancelledError):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                        stop_event=stop_event,
                    )

            # output 은 cancel 직후 작성되지 않음
            self.assertFalse(out.exists())

    def test_generate_note_does_not_retry_on_bad_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            client = MagicMock()
            err = openai.BadRequestError(
                message="bad",
                response=MagicMock(),
                body=None,
            )
            client.chat.completions.create.side_effect = err

            with patch.object(note_generator, "_create_client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "API 오류"):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                    )

            # 재시도 없이 즉시 raise (1회만 호출)
            self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_generate_note_retries_then_raises_on_persistent_api_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            client = MagicMock()
            err = openai.APIError(
                message="boom",
                request=MagicMock(),
                body=None,
            )
            client.chat.completions.create.side_effect = err

            with patch.object(note_generator, "_create_client", return_value=client), \
                 patch.object(note_generator.time, "sleep"):  # 재시도 backoff 무시
                with self.assertRaisesRegex(RuntimeError, "API 호출 .* 실패"):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                    )

            self.assertEqual(client.chat.completions.create.call_count, note_generator.MAX_RETRIES)

    def test_generate_note_omits_temperature_for_naver_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            client = MagicMock()
            client.chat.completions.create.return_value = _make_chat_response("# T\n본문")

            with patch.object(note_generator, "_create_client", return_value=client):
                note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                    provider="naver",
                    naver_api_key="key",
                )

            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertNotIn("temperature", kwargs)

    def test_generate_note_passes_temperature_for_openai_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            client = MagicMock()
            client.chat.completions.create.return_value = _make_chat_response("# T\n본문")

            with patch.object(note_generator, "_create_client", return_value=client):
                note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                    provider="openai",
                )

            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs.get("temperature"), 0.3)


if __name__ == "__main__":
    unittest.main()
