import logging
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from audio_extractor import extract_audio
from note_generator import generate_note
from transcriber import transcribe

logger = logging.getLogger(__name__)

STEP_NAMES = [
    "폴더 생성 및 파일 이동",
    "음성 추출",
    "STT 처리",
    "회의록 생성",
    "임시 파일 정리",
]


def run_pipeline(
    mp4_path: str,
    config: dict,
    status_callback: Optional[Callable[[str], None]] = None,
    confirm_callback: Optional[Callable[[str], bool]] = None,
) -> str:
    mp4 = Path(mp4_path)
    original_filename = mp4.name
    stem = mp4.stem

    watch_dir = Path(config.get("watch_dir", "~/Desktop")).expanduser()
    whisper_model = config.get("whisper_model", "medium")
    whisper_quant = config.get("whisper_quant", None)
    whisper_batch_size = config.get("whisper_batch_size", 12)
    language = config.get("language", "ko")
    openai_model = config.get("openai_model", "gpt-5.4")

    work_dir = watch_dir / stem
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _notify(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)

    try:
        # 1. 폴더 생성 및 파일 이동
        _notify(f"[1/5] {STEP_NAMES[0]}")
        work_dir.mkdir(parents=True, exist_ok=True)
        moved_mp4 = work_dir / mp4.name
        if mp4.exists():
            shutil.move(str(mp4), str(moved_mp4))
        elif moved_mp4.exists():
            pass  # 이미 이동됨
        else:
            raise FileNotFoundError(f"MP4 파일을 찾을 수 없습니다: {mp4_path}")

        # 2. 음성 추출
        wav_path = str(work_dir / "audio.wav")
        if Path(wav_path).exists() and confirm_callback and not confirm_callback(
            "audio.wav가 이미 존재합니다.\n다시 음성을 추출하시겠습니까?"
        ):
            _notify("[2/5] 음성 추출 건너뜀 (기존 파일 사용)")
        else:
            _notify(f"[2/5] {STEP_NAMES[1]}")
            extract_audio(str(moved_mp4), wav_path, progress_callback=_notify)

        # 3. STT 처리
        script_path = str(work_dir / "script.md")
        skip_stt = Path(script_path).exists() and confirm_callback is not None and not confirm_callback(
            "script.md가 이미 존재합니다.\n다시 STT를 처리하시겠습니까?"
        )
        if skip_stt:
            _notify("[3/5] STT 건너뜀 (기존 파일 사용)")
        else:
            _notify(f"[3/5] {STEP_NAMES[2]}")
            transcribe(
                wav_path,
                script_path,
                original_filename,
                model_name=whisper_model,
                quant=whisper_quant,
                batch_size=whisper_batch_size,
                language=language,
                progress_callback=_notify,
            )

        # 4. 회의록 생성
        _notify(f"[4/5] {STEP_NAMES[3]}")
        note_path = str(work_dir / "meeting_note.md")
        generate_note(
            script_path,
            note_path,
            original_filename,
            created_at,
            model=openai_model,
            progress_callback=_notify,
        )

        # 5. 임시 파일 정리
        _notify(f"[5/5] {STEP_NAMES[4]}")
        wav_file = Path(wav_path)
        if wav_file.exists():
            wav_file.unlink()
            logger.info("임시 WAV 파일 삭제: %s", wav_file.name)

        _notify(f"✅ 완료: {original_filename}")
        return str(work_dir)

    except Exception as e:
        error_msg = f"파이프라인 오류 ({original_filename}): {e}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())

        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            error_log = work_dir / "error.log"
            error_log.write_text(
                f"시각: {datetime.now().isoformat()}\n"
                f"파일: {original_filename}\n"
                f"오류: {e}\n\n"
                f"{traceback.format_exc()}",
                encoding="utf-8",
            )
        except Exception:
            logger.error("error.log 작성 실패")

        _notify(f"❌ 오류: {original_filename}")
        raise
