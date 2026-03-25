import logging
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Optional
import re


class PipelineCancelledError(Exception):
    pass

from audio_extractor import extract_audio
from audio_preprocessor import preprocess_audio
from note_generator import generate_note
from transcriber import transcribe

DICT_PATH = Path(__file__).parent / "dictionary.txt"

logger = logging.getLogger(__name__)

STEP_NAMES = [
    "폴더 생성 및 파일 이동",
    "음성 추출",
    "음성 전처리 (노이즈·침묵 제거 / 음량 정규화)",
    "STT 처리",
    "회의록 생성",
    "완료",
]


def run_pipeline(
    mp4_path: str,
    config: dict,
    status_callback: Optional[Callable[[str], None]] = None,
    confirm_callback: Optional[Callable[[str], bool]] = None,
    stop_event: Optional[Event] = None,
    pause_event: Optional[Event] = None,
) -> str:
    mp4 = Path(mp4_path)
    original_filename = mp4.name
    stem = mp4.stem
    is_audio_only = mp4.suffix.lower() == ".wav"

    watch_dir = Path(config.get("watch_dir", "~/Desktop")).expanduser()
    whisper_model = config.get("whisper_model", "medium")
    whisper_quant = config.get("whisper_quant", None)
    whisper_batch_size = config.get("whisper_batch_size", 12)
    language = config.get("language", "ko")
    openai_model = config.get("openai_model", "gpt-5.4")

    work_dir = watch_dir / stem
    moved_mp4 = work_dir / mp4.name
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _notify(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)

    def _check_stop():
        if stop_event is not None and stop_event.is_set():
            raise PipelineCancelledError("처리가 중단되었습니다.")

    def _check_pause():
        """일시중단 이벤트가 해제되어 있으면 재개될 때까지 대기. 대기 중에도 stop 확인."""
        if pause_event is None:
            _check_stop()
            return
        while not pause_event.wait(timeout=0.5):
            _check_stop()
        _check_stop()

    try:
        # 1. 폴더 생성 및 파일 이동
        _notify(f"[1/5] {STEP_NAMES[0]}")
        work_dir.mkdir(parents=True, exist_ok=True)
        if mp4.exists():
            shutil.move(str(mp4), str(moved_mp4))
        elif moved_mp4.exists():
            pass  # 이미 이동됨
        else:
            raise FileNotFoundError(f"MP4 파일을 찾을 수 없습니다: {mp4_path}")

        _check_pause()
        # 2. 음성 추출
        if is_audio_only:
            # WAV 직접 입력 (녹음 기능) — 음성 추출 건너뜀
            wav_path = str(moved_mp4)
            _notify("[2/6] 음성 추출 건너뜀 (오디오 파일 직접 입력)")
        else:
            wav_path = str(work_dir / "audio.wav")
            if Path(wav_path).exists() and confirm_callback and not confirm_callback(
                "audio.wav가 이미 존재합니다.\n다시 음성을 추출하시겠습니까?"
            ):
                _notify("[2/6] 음성 추출 건너뜀 (기존 파일 사용)")
            else:
                _notify(f"[2/6] {STEP_NAMES[1]}")
                extract_audio(str(moved_mp4), wav_path, progress_callback=_notify)

        _check_pause()
        # 3. 음성 전처리
        preprocessed_wav_path = str(work_dir / "audio_preprocessed.wav")
        _notify(f"[3/6] {STEP_NAMES[2]}")
        preprocess_audio(
            wav_path,
            preprocessed_wav_path,
            progress_callback=_notify,
            noise_reduce=config.get("preprocess_noise_reduce", True),
            vad=config.get("preprocess_vad", True),
            normalize=config.get("preprocess_normalize", True),
        )
        stt_input_path = preprocessed_wav_path

        _check_pause()
        # 4. STT 처리
        script_path = str(work_dir / f"{stem}_script.md")
        skip_stt = Path(script_path).exists() and confirm_callback is not None and not confirm_callback(
            "script.md가 이미 존재합니다.\n다시 STT를 처리하시겠습니까?"
        )
        if skip_stt:
            _notify("[4/6] STT 건너뜀 (기존 파일 사용)")
        else:
            _notify(f"[4/6] {STEP_NAMES[3]}")
            initial_prompt = None
            if DICT_PATH.exists():
                words = [w.strip() for w in DICT_PATH.read_text(encoding="utf-8").splitlines() if w.strip()]
                # Whisper initial_prompt 224토큰 한도 내로 제한 (단어당 평균 2토큰 가정 → 100단어)
                prompt = ""
                for w in words:
                    candidate = (prompt + ", " + w) if prompt else w
                    if len(candidate.encode("utf-8")) > 400:  # 약 100~150단어 수준
                        break
                    prompt = candidate
                initial_prompt = prompt or None
                if initial_prompt:
                    logger.info("STT initial_prompt 로드: %d개 단어 (dictionary.txt)", initial_prompt.count(",") + 1)
            transcribe(
                stt_input_path,
                script_path,
                original_filename,
                model_name=whisper_model,
                quant=whisper_quant,
                batch_size=whisper_batch_size,
                language=language,
                progress_callback=_notify,
                initial_prompt=initial_prompt,
            )

        _check_pause()
        # 5. 회의록 생성
        _notify(f"[5/6] {STEP_NAMES[4]}")
        note_tmp = str(work_dir / "_meeting_note_tmp.md")
        _, title = generate_note(
            script_path,
            note_tmp,
            original_filename,
            created_at,
            model=openai_model,
            progress_callback=_notify,
        )
        # 파일명: yyyy-mm-dd_hh_[제목]
        # 폴더명 예: "2026-03-20 15-03-21_회의내용"
        m = re.match(r'(\d{4}-\d{2}-\d{2})[\s_-](\d{2})', stem)
        if m:
            note_filename = f"{m.group(1)}_{m.group(2)}_{title}.md"
        else:
            note_filename = f"{stem}_{title}.md"
        note_path = str(work_dir / note_filename)
        Path(note_tmp).rename(note_path)

        # 6. 완료 (모든 파이프라인 단계 성공)
        _notify(f"[6/6] {STEP_NAMES[5]}")
        _notify(f"✅ 완료: {original_filename}")

        # 7. 회의록 내보내기 — 완료 후에만 실행, 실패해도 파이프라인 결과에 영향 없음
        export_dir_raw = config.get("export_dir", "~/Downloads")
        if export_dir_raw:
            try:
                export_dir = Path(export_dir_raw).expanduser()
                export_dir.mkdir(parents=True, exist_ok=True)
                export_dest = export_dir / Path(note_path).name
                shutil.copy2(note_path, str(export_dest))
                logger.info("회의록 내보내기 완료: %s", export_dest)
            except Exception as export_err:
                logger.error("회의록 내보내기 실패 (파이프라인 결과에는 영향 없음): %s", export_err)

        return str(work_dir)

    except PipelineCancelledError:
        logger.info("파이프라인 중단: %s", original_filename)
        # 파일을 원래 위치로 복원
        if moved_mp4.exists() and not mp4.exists():
            try:
                shutil.move(str(moved_mp4), str(mp4))
                logger.info("파일 복원: %s → %s", moved_mp4, mp4)
            except Exception as restore_err:
                logger.error("파일 복원 실패: %s", restore_err)
        # 작업 디렉토리가 비어 있으면 제거
        try:
            if work_dir.exists() and not any(work_dir.iterdir()):
                work_dir.rmdir()
        except OSError:
            pass
        raise

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
