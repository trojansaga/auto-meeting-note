import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import note_generator
from cancellation import OperationCancelledError


def _delta_line(text: str) -> str:
    """stream-json `stream_event`(content_block_delta) 한 줄을 만든다."""
    return json.dumps({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    })


def _result_line(result_text, is_error: bool = False) -> str:
    """stream-json 최종 `result` 이벤트 한 줄을 만든다."""
    return json.dumps({
        "type": "result",
        "result": result_text,
        "is_error": is_error,
    })


class _FakeStdin:
    """proc.stdin 역할: write/close 호출을 기록한다."""

    def __init__(self):
        self.written = ""
        self.closed = False

    def write(self, s):
        self.written += s

    def close(self):
        self.closed = True


class FakeProcess:
    """subprocess.Popen 반환값을 대체하는 fake 프로세스.

    stdout_lines: stream-json 라인(str) 리스트. 각 원소는 json.dumps 결과 문자열이며
    _run_claude_cli 쪽에서 line.strip() 후 json.loads 한다.
    """

    def __init__(self, stdout_lines, returncode: int = 0, stderr_text: str = ""):
        self.stdin = _FakeStdin()
        self._stdout_lines = list(stdout_lines)
        self.stdout = iter(self._stdout_lines)
        self._stderr_text = stderr_text
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = []

    # --- stderr는 read() 한 번만 호출되므로 간단한 객체로 대체 ---
    class _Stderr:
        def __init__(self, text):
            self._text = text

        def read(self):
            return self._text

    @property
    def stderr(self):
        return FakeProcess._Stderr(self._stderr_text)

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


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

    def test_extract_title_returns_default_for_empty_string(self):
        self.assertEqual(note_generator._extract_title(""), "회의록")

    def test_extract_title_clamps_to_50_chars(self):
        long = "# " + ("가" * 80)
        self.assertEqual(len(note_generator._extract_title(long)), 50)

    def test_extract_title_handles_crlf_line_endings(self):
        content = "# CRLF 제목\r\n본문\r\n"
        self.assertEqual(note_generator._extract_title(content), "CRLF 제목")


class FindClaudeCliTests(unittest.TestCase):
    def test_find_claude_cli_prefers_path_lookup(self):
        with patch.object(note_generator.shutil, "which", return_value="/usr/bin/claude"):
            self.assertEqual(note_generator.find_claude_cli(), "/usr/bin/claude")

    def test_find_claude_cli_returns_none_when_not_found_anywhere(self):
        with patch.object(note_generator.shutil, "which", return_value=None), \
             patch.object(note_generator.Path, "exists", return_value=False):
            self.assertIsNone(note_generator.find_claude_cli())


class GenerateNoteTests(unittest.TestCase):
    def _setup_script(self, tmp: str, content: str = "회의 대본 내용") -> tuple:
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

    def test_generate_note_raises_runtime_error_when_cli_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            with patch.object(note_generator, "find_claude_cli", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "claude CLI"):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                    )
            self.assertFalse(out.exists())

    def test_generate_note_success_writes_output_and_reports_full_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            fake_proc = FakeProcess(stdout_lines=[
                _delta_line("# 결정사항\n"),
                _delta_line("본문 1\n"),
                _delta_line("본문 2"),
                _result_line("# 결정사항\n본문 1\n본문 2"),
            ])

            statuses = []
            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc) as mock_popen:
                path_str, title = note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                    model="opus",
                    progress_callback=statuses.append,
                )

            self.assertEqual(path_str, str(out))
            self.assertEqual(title, "결정사항")
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("# 결정사항", content)
            self.assertIn("본문 2", content)

            # 진행률 콜백: 중간 진행 메시지와 100% 완료 메시지 모두 보고됨
            joined = "\n".join(statuses)
            self.assertIn("회의록 생성 중", joined)
            self.assertIn("100%", joined)
            self.assertEqual(statuses[-1], "[5/6] 회의록 생성 완료 (100%)")

            # subprocess.Popen이 정확히 한 번 호출됨
            self.assertEqual(mock_popen.call_count, 1)

    def test_generate_note_command_includes_model_print_and_empty_allowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            fake_proc = FakeProcess(stdout_lines=[
                _result_line("# 제목\n본문"),
            ])

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc) as mock_popen:
                note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                    model="sonnet",
                )

            argv = mock_popen.call_args.args[0]
            self.assertIn("--print", argv)
            self.assertIn("--model", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
            self.assertIn("--allowedTools", argv)
            self.assertEqual(argv[argv.index("--allowedTools") + 1], "")

    def test_generate_note_pipes_script_content_via_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_content = "[00:00:05] 회의를 시작합니다.\n[00:01:23] A안으로 진행합니다.\n"
            script, out = self._setup_script(tmp, content=script_content)
            fake_proc = FakeProcess(stdout_lines=[
                _result_line("# 제목\n본문"),
            ])

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc):
                note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

            stdin_written = fake_proc.stdin.written
            self.assertTrue(fake_proc.stdin.closed)
            # 대본 원문(타임스탬프 포함)이 stdin으로 전달된 user 프롬프트에 포함됨
            self.assertIn("[00:00:05] 회의를 시작합니다.", stdin_written)
            self.assertIn("[00:01:23] A안으로 진행합니다.", stdin_written)
            self.assertIn("demo.mp4", stdin_written)
            self.assertIn("2026-05-04", stdin_written)

    def test_generate_note_cancels_mid_stream_terminates_process_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)
            stop_event = threading.Event()

            fake_proc = FakeProcess(stdout_lines=[
                _delta_line("첫 번째 조각"),
                _delta_line("이건 처리되면 안 됨"),
                _result_line("# 제목\n본문"),
            ])

            def progress_callback(_msg):
                # 첫 진행 메시지 수신 직후 취소를 발화시킨다.
                stop_event.set()

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc):
                with self.assertRaises(OperationCancelledError):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                        progress_callback=progress_callback,
                        stop_event=stop_event,
                    )

            self.assertTrue(fake_proc.terminate_called)
            self.assertFalse(out.exists())

    def test_generate_note_retries_exhausted_then_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)

            fake_procs = [
                FakeProcess(stdout_lines=[], returncode=1, stderr_text="boom")
                for _ in range(note_generator.MAX_RETRIES)
            ]

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", side_effect=fake_procs) as mock_popen, \
                 patch.object(note_generator.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, f"{note_generator.MAX_RETRIES}회"):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                    )

            self.assertEqual(mock_popen.call_count, note_generator.MAX_RETRIES)
            self.assertFalse(out.exists())

    def test_generate_note_retries_on_is_error_result_then_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)

            fake_procs = [
                FakeProcess(stdout_lines=[_result_line("오류 메시지", is_error=True)], returncode=0)
                for _ in range(note_generator.MAX_RETRIES)
            ]

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", side_effect=fake_procs) as mock_popen, \
                 patch.object(note_generator.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    note_generator.generate_note(
                        str(script),
                        str(out),
                        "demo.mp4",
                        "2026-05-04",
                    )

            self.assertEqual(mock_popen.call_count, note_generator.MAX_RETRIES)

    def test_generate_note_succeeds_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = self._setup_script(tmp)

            fake_procs = [
                FakeProcess(stdout_lines=[], returncode=1, stderr_text="일시적 오류"),
                FakeProcess(stdout_lines=[_result_line("# 제목\n본문")], returncode=0),
            ]

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", side_effect=fake_procs) as mock_popen, \
                 patch.object(note_generator.time, "sleep"):
                path_str, title = note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

            self.assertEqual(mock_popen.call_count, 2)
            self.assertEqual(title, "제목")
            self.assertTrue(out.exists())

    def test_generate_note_with_script_lacking_timestamps_formats_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_content = "타임스탬프가 전혀 없는 회의 대본입니다. 그냥 텍스트만 있습니다."
            script, out = self._setup_script(tmp, content=script_content)
            fake_proc = FakeProcess(stdout_lines=[
                _result_line("# 제목\n본문"),
            ])

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc):
                path_str, title = note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

            self.assertEqual(path_str, str(out))
            self.assertTrue(out.exists())
            self.assertIn(script_content, fake_proc.stdin.written)


class SourceCitationPromptTests(unittest.TestCase):
    """출처(타임스탬프) 표기 프롬프트 지시 검증.

    아젠다는 스페이스 3칸 들여쓰기 + 항목 사이 빈 줄로 구분, 결정사항/다음 액션/기타 메모는
    탭(tab) 문자로 들여쓴 출처 하위 불릿을 사용한다. 복수 출처는 쉼표 나열이 아닌 하위 불릿을
    여러 줄로 열거한다. CLI 전환(subprocess 기반) 이후에도 이 프롬프트 내용은 바이트 단위로
    유지되어야 하며, stdin으로 전달되는 user 프롬프트에도 그대로 실려야 한다.
    """

    _SECTION_HEADERS = ("아젠다", "결정사항", "다음 액션", "기타 메모")
    _TAB_SECTIONS = ("결정사항", "다음 액션", "기타 메모")

    _AGENDA_SOURCE_LITERAL = '   - 출처: [HH:MM:SS] "인용된 대본 발언 내용"'
    _AGENDA_SOURCE_RE = re.compile(r'^ {3}- 출처: \[HH:MM:SS\] ".+"\s*$')
    _TAB_SOURCE_LITERAL = '\t- 출처: [HH:MM:SS] "인용된 대본 발언 내용"'
    _TAB_SOURCE_RE = re.compile(r'^\t- 출처: \[HH:MM:SS\] ".+"\s*$')
    _LEGACY_INLINE_SOURCE = "(출처:"

    def _section_body(self, template: str, header: str) -> str:
        m = re.search(rf"## {re.escape(header)}\n(.*?)(?=\n## |\Z)", template, re.DOTALL)
        self.assertIsNotNone(m, f"'{header}' 섹션을 USER_PROMPT_TEMPLATE에서 찾을 수 없음")
        return m.group(1)

    def _non_blank_lines(self, body: str) -> list:
        return [line for line in body.split("\n") if line.strip() != ""]

    def _item_blocks(self, body: str) -> list:
        blocks = []
        current = []
        for line in body.split("\n"):
            if line.strip() == "":
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)
        return blocks

    def test_system_prompt_contains_source_citation_anchors(self):
        self.assertIn("출처", note_generator.SYSTEM_PROMPT)
        self.assertIn("[HH:MM:SS]", note_generator.SYSTEM_PROMPT)

    def test_system_prompt_instructs_no_hallucination_and_omission_rule(self):
        self.assertIn("실제", note_generator.SYSTEM_PROMPT)
        self.assertIn("생략", note_generator.SYSTEM_PROMPT)

    def test_system_prompt_distinguishes_agenda_space_indent_from_tab_indent(self):
        self.assertIn(self._AGENDA_SOURCE_LITERAL, note_generator.SYSTEM_PROMPT)
        self.assertIn("탭", note_generator.SYSTEM_PROMPT)
        self.assertNotIn(self._LEGACY_INLINE_SOURCE, note_generator.SYSTEM_PROMPT)

    def test_system_prompt_instructs_blank_line_between_agenda_items(self):
        self.assertIn("빈 줄", note_generator.SYSTEM_PROMPT)

    def test_user_prompt_template_has_no_legacy_inline_source_format(self):
        self.assertNotIn(self._LEGACY_INLINE_SOURCE, note_generator.USER_PROMPT_TEMPLATE)

    def test_user_prompt_template_has_source_example_for_every_section(self):
        template = note_generator.USER_PROMPT_TEMPLATE
        self.assertIn("출처", template)
        self.assertIn("[HH:MM:SS]", template)

        agenda_body = self._section_body(template, "아젠다")
        self.assertIn(self._AGENDA_SOURCE_LITERAL, agenda_body)
        self.assertNotIn(self._LEGACY_INLINE_SOURCE, agenda_body)

        for header in self._TAB_SECTIONS:
            body = self._section_body(template, header)
            self.assertIn(self._TAB_SOURCE_LITERAL, body, f"'{header}' 섹션에 탭 들여쓴 출처 하위 불릿 예시가 없음")
            self.assertIn("[HH:MM:SS]", body)
            self.assertNotIn(self._LEGACY_INLINE_SOURCE, body)

    def test_user_prompt_template_source_sub_bullet_indented_directly_under_item_line(self):
        template = note_generator.USER_PROMPT_TEMPLATE

        agenda_body = self._section_body(template, "아젠다")
        agenda_blocks = self._item_blocks(agenda_body)
        self.assertGreaterEqual(len(agenda_blocks), 2, "아젠다 섹션에 빈 줄로 구분된 항목 블록이 최소 2개 있어야 함")
        for block in agenda_blocks:
            item_line, source_lines = block[0], block[1:]
            self.assertFalse(item_line.startswith(" "), f"아젠다 항목 라인이 들여써져 있음: {item_line!r}")
            self.assertGreaterEqual(len(source_lines), 1)
            for source_line in source_lines:
                self.assertRegex(source_line, self._AGENDA_SOURCE_RE)

        for header in self._TAB_SECTIONS:
            body = self._section_body(template, header)
            blocks = self._item_blocks(body)
            self.assertEqual(len(blocks), 1, f"'{header}' 섹션은 항목 블록이 하나여야 함")
            item_line, source_lines = blocks[0][0], blocks[0][1:]
            self.assertFalse(
                item_line.startswith("\t") or item_line.startswith(" "),
                f"'{header}' 섹션의 항목 라인이 들여써져 있음: {item_line!r}",
            )
            self.assertGreaterEqual(len(source_lines), 1)
            for source_line in source_lines:
                self.assertRegex(source_line, self._TAB_SOURCE_RE)

    def test_user_prompt_template_agenda_multiple_sources_enumerated_as_separate_lines(self):
        body = self._section_body(note_generator.USER_PROMPT_TEMPLATE, "아젠다")
        source_lines = [
            line for line in self._non_blank_lines(body)
            if line.strip().startswith("- 출처:")
        ]
        self.assertGreaterEqual(len(source_lines), 2)
        for source_line in source_lines:
            self.assertRegex(source_line, self._AGENDA_SOURCE_RE)
            self.assertNotIn(",", source_line)

    def test_next_action_section_keeps_owner_content_deadline_format(self):
        body = self._section_body(note_generator.USER_PROMPT_TEMPLATE, "다음 액션")
        self.assertIn("- [ ] 담당자 - 내용 - 기한", body)
        self.assertIn(self._TAB_SOURCE_LITERAL, body)
        self.assertNotIn(self._LEGACY_INLINE_SOURCE, body)

    def test_user_prompt_template_has_dedicated_source_rules_section(self):
        template = note_generator.USER_PROMPT_TEMPLATE
        self.assertIn("출처 표기 규칙", template)
        self.assertIn("환각", template)
        self.assertIn("생략", template)

    def test_user_prompt_template_source_rules_instruct_multiline_enumeration_not_csv(self):
        template = note_generator.USER_PROMPT_TEMPLATE
        m = re.search(r"## 출처 표기 규칙\n(.*?)(?=\n---|\Z)", template, re.DOTALL)
        self.assertIsNotNone(m, "'출처 표기 규칙' 섹션을 찾을 수 없음")
        rules_body = m.group(1)

        self.assertIn("스페이스 3칸", rules_body)
        self.assertIn("탭", rules_body)
        self.assertIn("빈 줄", rules_body)
        self.assertIn("쉼표", rules_body)
        self.assertIn("여러 개", rules_body)
        self.assertIn('"', rules_body)
        self.assertNotIn(self._LEGACY_INLINE_SOURCE, rules_body)

    def test_user_prompt_template_has_exactly_three_placeholders(self):
        rendered = note_generator.USER_PROMPT_TEMPLATE.format(
            filename="demo.mp4",
            datetime="2026-05-04",
            script="본문",
        )
        self.assertIn("demo.mp4", rendered)
        self.assertIn("2026-05-04", rendered)
        self.assertIn("본문", rendered)

    def test_generate_note_sends_timestamped_script_and_source_instructions_via_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_content = (
                "[00:00:05] 회의를 시작합니다.\n"
                "[00:01:23] A안으로 진행하기로 결정했습니다.\n"
                "[00:05:10] 홍길동님이 다음 주 금요일까지 문서를 작성합니다.\n"
            )
            script = Path(tmp) / "script.md"
            script.write_text(script_content, encoding="utf-8")
            out = Path(tmp) / "out" / "note.md"

            fake_proc = FakeProcess(stdout_lines=[_result_line("# 제목\n본문")])

            with patch.object(note_generator, "find_claude_cli", return_value="/fake/claude"), \
                 patch.object(note_generator.subprocess, "Popen", return_value=fake_proc):
                note_generator.generate_note(
                    str(script),
                    str(out),
                    "demo.mp4",
                    "2026-05-04",
                )

            stdin_written = fake_proc.stdin.written

            self.assertIn("[00:01:23] A안으로 진행하기로 결정했습니다.", stdin_written)
            self.assertIn("[00:05:10] 홍길동님이 다음 주 금요일까지 문서를 작성합니다.", stdin_written)
            self.assertIn("출처", stdin_written)
            self.assertIn("[HH:MM:SS]", stdin_written)
            self.assertIn(self._AGENDA_SOURCE_LITERAL, stdin_written)
            self.assertIn(self._TAB_SOURCE_LITERAL, stdin_written)
            self.assertNotIn(self._LEGACY_INLINE_SOURCE, stdin_written)
            for header in self._SECTION_HEADERS:
                self.assertIn(f"## {header}", stdin_written)


if __name__ == "__main__":
    unittest.main()
