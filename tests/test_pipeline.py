import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline


def _stub_generate_note(_script, note_tmp, *_args, **_kwargs):
    Path(note_tmp).write_text("# note\n", encoding="utf-8")
    return note_tmp, "주제"


def _stub_transcribe(_wav, script_path, *_args, **_kwargs):
    Path(script_path).write_text("transcribed", encoding="utf-8")


def _stub_extract_audio(_mp4, wav_path, *_args, **_kwargs):
    Path(wav_path).write_bytes(b"WAVE")


def _stub_preprocess_audio(_in, out_path, *_args, **_kwargs):
    Path(out_path).write_bytes(b"WAVE-pre")


class _TempEnv:
    """TemporaryDirectory 두 개(watch_dir, export_dir) + mp4 파일 준비."""

    def __init__(self, suffix=".mp4"):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch_dir = Path(self.tmp.name) / "watch"
        self.export_dir = Path(self.tmp.name) / "export"
        self.source_dir = Path(self.tmp.name) / "src"
        self.source_dir.mkdir()
        # 폴더명에 날짜 추출 정규식 매칭되도록 prefix
        self.mp4 = self.source_dir / f"2026-05-04 12-00-00_demo{suffix}"
        self.mp4.write_bytes(b"FAKE-MP4")

    def base_config(self):
        return {
            "watch_dir": str(self.watch_dir),
            "export_dir": str(self.export_dir),
            "stt_backend": "whisper",
            "language": "ko",
            "claude_cli_model": "opus",
        }

    def cleanup(self):
        self.tmp.cleanup()


class PipelineHappyPathTests(unittest.TestCase):
    def test_run_pipeline_with_mp4_calls_each_stage_in_order(self):
        env = _TempEnv()
        try:
            statuses: list[str] = []

            with patch.object(pipeline, "extract_audio", side_effect=_stub_extract_audio) as m_extract, \
                 patch.object(pipeline, "preprocess_audio", side_effect=_stub_preprocess_audio) as m_pre, \
                 patch.object(pipeline, "transcribe", side_effect=_stub_transcribe) as m_tr, \
                 patch.object(pipeline, "generate_note", side_effect=_stub_generate_note) as m_note:
                result = pipeline.run_pipeline(
                    str(env.mp4),
                    env.base_config(),
                    status_callback=statuses.append,
                )

            # 각 단계 1회씩 호출
            self.assertEqual(m_extract.call_count, 1)
            self.assertEqual(m_pre.call_count, 1)
            self.assertEqual(m_tr.call_count, 1)
            self.assertEqual(m_note.call_count, 1)

            # generate_note는 config의 claude_cli_model 값을 model kwarg로만 전달받고
            # provider/naver 관련 인자는 전혀 넘기지 않는다
            note_kwargs = m_note.call_args.kwargs
            self.assertEqual(note_kwargs.get("model"), "opus")
            for forbidden_kwarg in ("provider", "naver_api_key", "naver_api_base_url"):
                self.assertNotIn(forbidden_kwarg, note_kwargs)

            # 진행 상태 메시지에 STT/회의록 단계 표시 포함
            joined = "\n".join(statuses)
            self.assertIn("[2/6]", joined)
            self.assertIn("[4/6]", joined)
            self.assertIn("[5/6]", joined)

            # 결과 폴더가 제목으로 rename 됨
            self.assertTrue(Path(result).exists())
            self.assertIn("주제", Path(result).name)

            # 회의록 내보내기 (export_dir) 동작
            exported = list(env.export_dir.glob("*.md"))
            self.assertEqual(len(exported), 1)
            self.assertIn("(자동회의록)", exported[0].name)
        finally:
            env.cleanup()

    def test_run_pipeline_with_wav_skips_audio_extraction(self):
        env = _TempEnv(suffix=".wav")
        try:
            with patch.object(pipeline, "extract_audio", side_effect=_stub_extract_audio) as m_extract, \
                 patch.object(pipeline, "preprocess_audio", side_effect=_stub_preprocess_audio) as m_pre, \
                 patch.object(pipeline, "transcribe", side_effect=_stub_transcribe), \
                 patch.object(pipeline, "generate_note", side_effect=_stub_generate_note):
                pipeline.run_pipeline(str(env.mp4), env.base_config())

            m_extract.assert_not_called()
            self.assertEqual(m_pre.call_count, 1)
        finally:
            env.cleanup()

    def test_claude_cli_model_config_value_is_threaded_to_generate_note(self):
        """config의 claude_cli_model이 기본값('opus')과 다른 임의 값이어도
        그대로 generate_note(model=...)에 전달되는지 검증 (하드코딩된 우연의 일치 방지)."""
        env = _TempEnv()
        try:
            cfg = env.base_config()
            cfg["claude_cli_model"] = "sonnet"  # 기본값(opus)과 다른 값

            with patch.object(pipeline, "extract_audio", side_effect=_stub_extract_audio), \
                 patch.object(pipeline, "preprocess_audio", side_effect=_stub_preprocess_audio), \
                 patch.object(pipeline, "transcribe", side_effect=_stub_transcribe), \
                 patch.object(pipeline, "generate_note", side_effect=_stub_generate_note) as m_note:
                pipeline.run_pipeline(str(env.mp4), cfg)

            self.assertEqual(m_note.call_args.kwargs.get("model"), "sonnet")
        finally:
            env.cleanup()

    def test_claude_cli_model_defaults_to_opus_when_unset(self):
        """config에 claude_cli_model 키가 아예 없으면 기본값 'opus'가 전달된다."""
        env = _TempEnv()
        try:
            cfg = env.base_config()
            del cfg["claude_cli_model"]

            with patch.object(pipeline, "extract_audio", side_effect=_stub_extract_audio), \
                 patch.object(pipeline, "preprocess_audio", side_effect=_stub_preprocess_audio), \
                 patch.object(pipeline, "transcribe", side_effect=_stub_transcribe), \
                 patch.object(pipeline, "generate_note", side_effect=_stub_generate_note) as m_note:
                pipeline.run_pipeline(str(env.mp4), cfg)

            self.assertEqual(m_note.call_args.kwargs.get("model"), "opus")
        finally:
            env.cleanup()

    def test_apple_speech_backend_skips_preprocessing(self):
        env = _TempEnv()
        try:
            cfg = env.base_config()
            cfg["stt_backend"] = "apple_speech"

            with patch.object(pipeline, "extract_audio", side_effect=_stub_extract_audio), \
                 patch.object(pipeline, "preprocess_audio", side_effect=_stub_preprocess_audio) as m_pre, \
                 patch.object(pipeline, "transcribe", side_effect=_stub_transcribe), \
                 patch.object(pipeline, "generate_note", side_effect=_stub_generate_note):
                pipeline.run_pipeline(str(env.mp4), cfg)

            m_pre.assert_not_called()
        finally:
            env.cleanup()


class PipelineCancellationTests(unittest.TestCase):
    def test_stop_event_raises_cancelled_and_restores_source_file(self):
        env = _TempEnv()
        try:
            stop_event = threading.Event()

            # extract_audio 진입 직전(_check_pause 단계)에 stop_event 발화하기 위해
            # extract_audio 가 호출되자마자 stop_event 를 set 하고 정상 반환
            def _extract_then_signal(_mp4, wav_path, *_args, **_kwargs):
                Path(wav_path).write_bytes(b"WAVE")
                stop_event.set()

            with patch.object(pipeline, "extract_audio", side_effect=_extract_then_signal), \
                 patch.object(pipeline, "preprocess_audio") as m_pre, \
                 patch.object(pipeline, "transcribe") as m_tr, \
                 patch.object(pipeline, "generate_note") as m_note:
                with self.assertRaises(pipeline.PipelineCancelledError):
                    pipeline.run_pipeline(
                        str(env.mp4),
                        env.base_config(),
                        stop_event=stop_event,
                    )

            # extract_audio 직후 _check_pause 에서 cancel 감지 → 이후 단계 미진입
            m_pre.assert_not_called()
            m_tr.assert_not_called()
            m_note.assert_not_called()

            # 원본 mp4 가 원래 위치로 복원
            self.assertTrue(env.mp4.exists())
        finally:
            env.cleanup()

    def test_pipeline_error_writes_error_log_in_work_dir(self):
        env = _TempEnv()
        try:
            with patch.object(
                pipeline, "extract_audio", side_effect=RuntimeError("ffmpeg 폭발")
            ), patch.object(pipeline, "preprocess_audio"), patch.object(
                pipeline, "transcribe"
            ), patch.object(pipeline, "generate_note"):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg 폭발"):
                    pipeline.run_pipeline(str(env.mp4), env.base_config())

            # work_dir 안에 error.log 작성됐는지
            work_dir = env.watch_dir / env.mp4.stem
            error_log = work_dir / "error.log"
            self.assertTrue(error_log.exists())
            content = error_log.read_text(encoding="utf-8")
            self.assertIn("ffmpeg 폭발", content)
            self.assertIn(env.mp4.name, content)
        finally:
            env.cleanup()


class PipelineWorkDirNamingTests(unittest.TestCase):
    def test_work_dir_with_title_appends_clean_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "2026-05-04 12-00-00_demo"
            renamed = pipeline._work_dir_with_title(base, "주제")
            self.assertEqual(renamed.name, "2026-05-04 12-00-00_demo_주제")

    def test_work_dir_with_title_avoids_existing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "demo"
            collide = base.with_name("demo_주제")
            collide.mkdir()
            renamed = pipeline._work_dir_with_title(base, "주제")
            # 충돌 시 _2 suffix 가 붙는다
            self.assertEqual(renamed.name, "demo_주제_2")

    def test_work_dir_with_title_returns_unchanged_when_already_suffixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "demo_주제"
            renamed = pipeline._work_dir_with_title(base, "주제")
            self.assertEqual(renamed, base)


if __name__ == "__main__":
    unittest.main()
