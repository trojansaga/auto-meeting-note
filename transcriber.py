import gc
import logging
import multiprocessing as mp
import queue
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Optional

from cancellation import OperationCancelledError

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

DEFAULT_STT_BACKEND = "whisper"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_WHISPER_QUANT = "4bit"
DEFAULT_QWEN_MODEL = "Qwen/Qwen3-ASR-0.6B"
DEFAULT_QWEN_CHUNK_SECONDS = 600

STT_BACKENDS = {
    "whisper": "Whisper (MLX)",
    "qwen3_asr": "Qwen3-ASR",
}

WHISPER_MODEL_REPOS = {
    "tiny": {"base": "mlx-community/whisper-tiny-mlx", "4bit": "mlx-community/whisper-tiny-mlx-4bit", "8bit": "mlx-community/whisper-tiny-mlx-8bit"},
    "small": {"base": "mlx-community/whisper-small-mlx", "4bit": "mlx-community/whisper-small-mlx-4bit", "8bit": "mlx-community/whisper-small-mlx-8bit"},
    "medium": {"base": "mlx-community/whisper-medium-mlx", "4bit": "mlx-community/whisper-medium-mlx-4bit", "8bit": "mlx-community/whisper-medium-mlx-8bit"},
    "large-v3": {"base": "mlx-community/whisper-large-v3-mlx", "4bit": "mlx-community/whisper-large-v3-mlx-4bit", "8bit": "mlx-community/whisper-large-v3-mlx-8bit"},
}

QWEN_MODEL_REPOS = {
    "Qwen3-ASR-0.6B": "Qwen/Qwen3-ASR-0.6B",
    "Qwen3-ASR-1.7B": "Qwen/Qwen3-ASR-1.7B",
}

QWEN_LANGUAGE_MAP = {
    "ar": "Arabic",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "mk": "Macedonian",
    "ms": "Malay",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
}


def get_backend_label(backend: str) -> str:
    return STT_BACKENDS.get(backend, backend)


def get_model_display_name(backend: str, model_name: str, quant: Optional[str] = None) -> str:
    if backend == "whisper":
        return f"{model_name} ({quant or 'base'})"
    return Path(model_name).name or model_name


def get_model_download_repo(backend: str, model_name: str, quant: Optional[str] = None) -> Optional[str]:
    if backend == "whisper":
        return _get_whisper_repo(model_name, quant)

    model_path = Path(model_name).expanduser()
    if model_path.exists():
        return None

    return model_name


def _get_whisper_repo(model_name: str, quant: Optional[str]) -> str:
    if model_name not in WHISPER_MODEL_REPOS:
        raise ValueError(f"지원하지 않는 Whisper 모델: {model_name}")
    variants = WHISPER_MODEL_REPOS[model_name]
    key = quant if quant in variants else "base"
    return variants[key]


def _normalize_qwen_language(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    normalized = language.strip()
    if not normalized:
        return None
    if normalized in QWEN_LANGUAGE_MAP.values():
        return normalized
    return QWEN_LANGUAGE_MAP.get(normalized.lower(), normalized)


def _resolve_torch_dtype(dtype_name: Optional[str]):
    if not dtype_name:
        return None

    import torch

    normalized = str(dtype_name).strip().lower()
    mapping = {
        "float16": torch.float16,
        "fp16": torch.float16,
        "half": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }
    if normalized not in mapping:
        raise ValueError(f"지원하지 않는 Qwen dtype: {dtype_name}")
    return mapping[normalized]


def _normalize_qwen_attn_implementation(attn_name: Optional[str]) -> Optional[str]:
    normalized = (attn_name or "").strip()
    return normalized or None


def _torch_dtype_name(dtype) -> str:
    if dtype is None:
        return "default"
    return str(dtype).replace("torch.", "")


def _resolve_qwen_runtime(
    qwen_dtype: Optional[str],
    qwen_device_map: Optional[str],
    qwen_attn_implementation: Optional[str],
) -> tuple[Optional[str], object, Optional[str], bool]:
    import torch

    explicit_device = (qwen_device_map or "").strip() or None
    explicit_dtype = _resolve_torch_dtype(qwen_dtype)
    explicit_attn = _normalize_qwen_attn_implementation(qwen_attn_implementation)

    auto_device = None
    auto_dtype = torch.float32
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        auto_device = "mps"
        auto_dtype = torch.float16
    elif torch.cuda.is_available():
        auto_device = "cuda:0"
        auto_dtype = torch.float16

    resolved_device = explicit_device if explicit_device is not None else auto_device
    resolved_dtype = explicit_dtype if explicit_dtype is not None else auto_dtype
    resolved_attn = explicit_attn if explicit_attn is not None else ("eager" if resolved_device == "mps" else None)
    used_auto_runtime = explicit_device is None and explicit_dtype is None and explicit_attn is None
    return resolved_device, resolved_dtype, resolved_attn, used_auto_runtime


def _resolve_qwen_chunk_seconds(chunk_seconds: Optional[int]) -> int:
    if chunk_seconds is None:
        return DEFAULT_QWEN_CHUNK_SECONDS
    return max(60, int(chunk_seconds))


def _format_qwen_runtime_summary(
    device_map: Optional[str],
    dtype_name: Optional[str],
    attn_implementation: Optional[str],
    chunk_seconds: int,
) -> str:
    return (
        f"device={device_map or 'cpu'}, "
        f"dtype={dtype_name or 'default'}, "
        f"attn={attn_implementation or 'default'}, "
        f"chunk={chunk_seconds}s"
    )


def _build_qwen_attempts(kwargs: dict) -> list[dict]:
    device_map, dtype, attn_implementation, _ = _resolve_qwen_runtime(
        kwargs.get("qwen_dtype"),
        kwargs.get("qwen_device_map"),
        kwargs.get("qwen_attn_implementation"),
    )
    chunk_seconds = _resolve_qwen_chunk_seconds(kwargs.get("qwen_chunk_seconds"))
    resolved_dtype_name = None if dtype is None else _torch_dtype_name(dtype)
    attempts = []
    seen = set()

    def _add_attempt(
        device_name: Optional[str],
        dtype_name: Optional[str],
        attn_name: Optional[str],
        retry_message: str,
    ):
        normalized_device = (device_name or "").strip() or "cpu"
        normalized_dtype = (dtype_name or "").strip() or None
        normalized_attn = _normalize_qwen_attn_implementation(attn_name)
        key = (normalized_device, normalized_dtype, normalized_attn, chunk_seconds)
        if key in seen:
            return
        seen.add(key)

        attempt_kwargs = dict(kwargs)
        attempt_kwargs["qwen_device_map"] = normalized_device
        attempt_kwargs["qwen_dtype"] = normalized_dtype
        attempt_kwargs["qwen_attn_implementation"] = normalized_attn
        attempt_kwargs["qwen_chunk_seconds"] = chunk_seconds
        attempts.append(
            {
                "device": normalized_device,
                "summary": _format_qwen_runtime_summary(
                    normalized_device,
                    normalized_dtype,
                    normalized_attn,
                    chunk_seconds,
                ),
                "retry_message": retry_message,
                "kwargs": attempt_kwargs,
            }
        )

    _add_attempt(device_map, resolved_dtype_name, attn_implementation, "Qwen3-ASR 실행 중...")

    if (device_map or "cpu") == "mps":
        _add_attempt("mps", "float32", attn_implementation, "Qwen3-ASR MPS 재시도 중... (float32)")
        _add_attempt("cpu", "float32", None, "Qwen3-ASR CPU 재시도 중...")

    return attempts


def _get_audio_duration_seconds(wav: Path) -> float:
    import soundfile as sf

    info = sf.info(str(wav))
    if info.samplerate <= 0:
        return 0.0
    return info.frames / float(info.samplerate)


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _build_output_lines(
    original_filename: str,
    recognized_language: Optional[str],
    transcript_lines: list[str],
) -> list[str]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 회의 대본\n",
        f"- 파일명: {original_filename}",
        f"- 생성일시: {now}",
    ]
    if recognized_language:
        lines.append(f"- 감지 언어: {recognized_language}")
    lines.extend(["", "## Transcript\n"])
    lines.extend(transcript_lines)
    return lines


def _write_output(output_path: str, lines: list[str]) -> str:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return str(output)


def _run_progress_loop(
    expected_secs: float,
    stop_event: threading.Event,
    progress_callback: Optional[Callable[[str], None]],
) -> Optional[threading.Thread]:
    if progress_callback is None:
        return None

    expected_secs = max(expected_secs, 1.0)

    def _progress_loop():
        start = time.time()
        last_message = None
        while not stop_event.is_set():
            elapsed = time.time() - start
            ratio = elapsed / expected_secs

            if ratio < 0.98:
                pct = max(1, min(ratio * 96, 96))
                message = f"[4/6] STT 처리 중... {pct:.0f}%"
            elif ratio < 1.25:
                message = "[4/6] STT 마무리 중..."
            else:
                message = f"[4/6] STT 마무리 중... 경과 {_format_elapsed(elapsed)}"

            if message != last_message:
                progress_callback(message)
                last_message = message

            if stop_event.wait(timeout=1):
                break

    thread = threading.Thread(target=_progress_loop, daemon=True)
    thread.start()
    return thread


def _get_expected_secs(backend: str, wav: Path) -> float:
    audio_duration = _get_audio_duration_seconds(wav)
    if backend == "whisper":
        return audio_duration / 10.0
    return audio_duration


def _clear_whisper_cache():
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder

    ModelHolder.model = None
    ModelHolder.model_path = None
    gc.collect()
    mx.metal.clear_cache()


def _clear_torch_cache():
    gc.collect()
    try:
        import torch
    except ImportError:
        return

    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    try:
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def _transcribe_with_whisper(
    wav: Path,
    output_path: str,
    original_filename: str,
    model_name: str,
    quant: Optional[str],
    batch_size: int,
    language: str,
    progress_callback: Optional[Callable[[str], None]],
    initial_prompt: Optional[str],
) -> str:
    import mlx_whisper

    repo = _get_whisper_repo(model_name, quant)
    logger.info("STT 처리 시작: %s (backend=whisper, repo=%s)", wav.name, repo)

    expected_secs = _get_expected_secs("whisper", wav)
    stop_event = threading.Event()
    progress_thread = _run_progress_loop(expected_secs, stop_event, progress_callback)
    transcribe_kwargs = {
        "path_or_hf_repo": repo,
        "language": language,
        "temperature": 0,
        "initial_prompt": initial_prompt or None,
        "condition_on_previous_text": False,
    }
    if batch_size:
        transcribe_kwargs["batch_size"] = max(1, int(batch_size))

    try:
        try:
            result = mlx_whisper.transcribe(str(wav), **transcribe_kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'batch_size'" not in str(exc):
                raise
            transcribe_kwargs.pop("batch_size", None)
            logger.warning("설치된 mlx_whisper 버전이 batch_size를 지원하지 않아 기본 설정으로 재시도합니다.")
            result = mlx_whisper.transcribe(str(wav), **transcribe_kwargs)
    finally:
        stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=1)

    if progress_callback:
        progress_callback("[4/6] STT 처리 완료 (100%)")

    transcript_lines = []
    for seg in result.get("segments", []):
        start_sec = seg.get("start", 0)
        text = seg.get("text", "").strip()
        if text:
            transcript_lines.append(f"{_format_timestamp(start_sec)} {text}")

    if not transcript_lines and result.get("text"):
        transcript_lines.append(result["text"].strip())

    lines = _build_output_lines(
        original_filename=original_filename,
        recognized_language=result.get("language"),
        transcript_lines=transcript_lines,
    )

    del result
    _clear_whisper_cache()

    output = _write_output(output_path, lines)
    logger.info("STT 처리 완료 → %s", Path(output).name)
    return output


def _transcribe_with_qwen(
    wav: Path,
    output_path: str,
    original_filename: str,
    model_name: str,
    batch_size: int,
    language: str,
    progress_callback: Optional[Callable[[str], None]],
    context_hint: Optional[str],
    qwen_dtype: Optional[str],
    qwen_device_map: Optional[str],
    qwen_attn_implementation: Optional[str],
    qwen_forced_aligner: Optional[str],
    qwen_return_timestamps: bool,
    qwen_max_new_tokens: int,
    qwen_chunk_seconds: Optional[int],
) -> str:
    from qwen_asr import Qwen3ASRModel
    from qwen_asr.inference import qwen3_asr as qwen3_asr_module
    from qwen_asr.inference import utils as qwen_utils

    logger.info("STT 처리 시작: %s (backend=qwen3_asr, model=%s)", wav.name, model_name)

    expected_secs = _get_expected_secs("qwen3_asr", wav)
    stop_event = threading.Event()
    progress_thread = _run_progress_loop(expected_secs, stop_event, progress_callback)

    device_map, dtype, attn_implementation, used_auto_runtime = _resolve_qwen_runtime(
        qwen_dtype,
        qwen_device_map,
        qwen_attn_implementation,
    )
    resolved_chunk_seconds = _resolve_qwen_chunk_seconds(qwen_chunk_seconds)
    resolved_batch_size = max(1, int(batch_size or 1))
    logger.info(
        "Qwen3-ASR 런타임 설정: device=%s, dtype=%s, attn=%s, chunk=%ss, batch=%s%s",
        device_map or "cpu",
        _torch_dtype_name(dtype),
        attn_implementation or "default",
        resolved_chunk_seconds,
        resolved_batch_size,
        " (auto)" if used_auto_runtime else "",
    )
    if device_map is None:
        logger.warning("Qwen3-ASR가 CPU에서 실행됩니다. Mac에서는 qwen_device_map='mps', qwen_dtype='float16'이 더 빠를 수 있습니다.")

    model_kwargs = {
        "max_inference_batch_size": resolved_batch_size,
        "max_new_tokens": max(256, int(qwen_max_new_tokens or 4096)),
    }
    if dtype is not None:
        model_kwargs["dtype"] = dtype
    if device_map is not None:
        model_kwargs["device_map"] = device_map
    if attn_implementation is not None:
        model_kwargs["attn_implementation"] = attn_implementation

    forced_aligner = (qwen_forced_aligner or "").strip() or None
    return_time_stamps = bool(qwen_return_timestamps and forced_aligner)
    if forced_aligner:
        model_kwargs["forced_aligner"] = forced_aligner
        aligner_kwargs = {}
        if dtype is not None:
            aligner_kwargs["dtype"] = dtype
        if device_map is not None:
            aligner_kwargs["device_map"] = device_map
        if aligner_kwargs:
            model_kwargs["forced_aligner_kwargs"] = aligner_kwargs

    original_max_asr = getattr(qwen3_asr_module, "MAX_ASR_INPUT_SECONDS", None)
    original_max_force_align = getattr(qwen3_asr_module, "MAX_FORCE_ALIGN_INPUT_SECONDS", None)
    original_utils_max_asr = getattr(qwen_utils, "MAX_ASR_INPUT_SECONDS", None)
    original_utils_max_force_align = getattr(qwen_utils, "MAX_FORCE_ALIGN_INPUT_SECONDS", None)

    try:
        qwen3_asr_module.MAX_ASR_INPUT_SECONDS = resolved_chunk_seconds
        qwen_utils.MAX_ASR_INPUT_SECONDS = resolved_chunk_seconds
        if original_max_force_align is not None:
            qwen3_asr_module.MAX_FORCE_ALIGN_INPUT_SECONDS = min(int(original_max_force_align), resolved_chunk_seconds)
        if original_utils_max_force_align is not None:
            qwen_utils.MAX_FORCE_ALIGN_INPUT_SECONDS = min(int(original_utils_max_force_align), resolved_chunk_seconds)

        try:
            model = Qwen3ASRModel.from_pretrained(model_name, **model_kwargs)
        except Exception:
            if not used_auto_runtime:
                raise
            import torch

            fallback_kwargs = dict(model_kwargs)
            fallback_kwargs.pop("device_map", None)
            fallback_kwargs["dtype"] = torch.float32
            logger.warning(
                "Qwen3-ASR 자동 런타임 초기화에 실패해 CPU/float32로 재시도합니다.",
                exc_info=True,
            )
            model = Qwen3ASRModel.from_pretrained(model_name, **fallback_kwargs)
        results = model.transcribe(
            audio=str(wav),
            context=context_hint or "",
            language=_normalize_qwen_language(language),
            return_time_stamps=return_time_stamps,
        )
    finally:
        if original_max_asr is not None:
            qwen3_asr_module.MAX_ASR_INPUT_SECONDS = original_max_asr
        if original_utils_max_asr is not None:
            qwen_utils.MAX_ASR_INPUT_SECONDS = original_utils_max_asr
        if original_max_force_align is not None:
            qwen3_asr_module.MAX_FORCE_ALIGN_INPUT_SECONDS = original_max_force_align
        if original_utils_max_force_align is not None:
            qwen_utils.MAX_FORCE_ALIGN_INPUT_SECONDS = original_utils_max_force_align
        stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=1)

    if progress_callback:
        progress_callback("[4/6] STT 처리 완료 (100%)")

    if not results:
        raise RuntimeError("Qwen3-ASR 결과가 비어 있습니다.")

    result = results[0]
    transcript_lines = []

    timestamps = getattr(result, "time_stamps", None) or []
    for item in timestamps:
        text = getattr(item, "text", "").strip()
        start_sec = float(getattr(item, "start_time", 0) or 0)
        if text:
            transcript_lines.append(f"{_format_timestamp(start_sec)} {text}")

    result_text = getattr(result, "text", "").strip()
    if not transcript_lines and result_text:
        transcript_lines.append(result_text)

    recognized_language = getattr(result, "language", None)
    lines = _build_output_lines(
        original_filename=original_filename,
        recognized_language=recognized_language,
        transcript_lines=transcript_lines,
    )

    del result
    del results
    del model
    _clear_torch_cache()

    output = _write_output(output_path, lines)
    logger.info("STT 처리 완료 → %s", Path(output).name)
    return output


def _transcribe_impl(
    wav_path: str,
    output_path: str,
    original_filename: str,
    backend: str,
    model_name: str,
    quant: Optional[str],
    batch_size: int,
    language: str,
    progress_callback: Optional[Callable[[str], None]],
    initial_prompt: Optional[str],
    qwen_dtype: Optional[str],
    qwen_device_map: Optional[str],
    qwen_attn_implementation: Optional[str],
    qwen_forced_aligner: Optional[str],
    qwen_return_timestamps: bool,
    qwen_max_new_tokens: int,
    qwen_chunk_seconds: Optional[int],
) -> str:
    wav = Path(wav_path)
    if not wav.exists():
        raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {wav_path}")

    if backend == "whisper":
        return _transcribe_with_whisper(
            wav=wav,
            output_path=output_path,
            original_filename=original_filename,
            model_name=model_name,
            quant=quant,
            batch_size=batch_size,
            language=language,
            progress_callback=progress_callback,
            initial_prompt=initial_prompt,
        )

    if backend == "qwen3_asr":
        return _transcribe_with_qwen(
            wav=wav,
            output_path=output_path,
            original_filename=original_filename,
            model_name=model_name,
            batch_size=batch_size,
            language=language,
            progress_callback=progress_callback,
            context_hint=initial_prompt,
            qwen_dtype=qwen_dtype,
            qwen_device_map=qwen_device_map,
            qwen_attn_implementation=qwen_attn_implementation,
            qwen_forced_aligner=qwen_forced_aligner,
            qwen_return_timestamps=qwen_return_timestamps,
            qwen_max_new_tokens=qwen_max_new_tokens,
            qwen_chunk_seconds=qwen_chunk_seconds,
        )

    raise ValueError(f"지원하지 않는 STT 백엔드: {backend}")


def _transcribe_worker(result_queue: mp.Queue, kwargs: dict):
    try:
        output = _transcribe_impl(progress_callback=None, **kwargs)
        result_queue.put(("ok", output))
    except Exception:
        result_queue.put(("err", traceback.format_exc()))


def _run_transcribe_attempt(
    ctx,
    stop_event: Event,
    output: Path,
    result_kwargs: dict,
) -> str:
    result_queue = ctx.Queue()
    process = ctx.Process(target=_transcribe_worker, args=(result_queue, result_kwargs), daemon=True)

    try:
        process.start()
        while True:
            if stop_event.is_set():
                logger.info("STT 중단 요청: %s", Path(result_kwargs["wav_path"]).name)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
                if output.exists():
                    output.unlink(missing_ok=True)
                raise OperationCancelledError("STT 처리가 중단되었습니다.")

            try:
                status, payload = result_queue.get(timeout=0.5)
            except queue.Empty:
                if process.is_alive():
                    continue
                try:
                    status, payload = result_queue.get_nowait()
                except queue.Empty:
                    raise RuntimeError(f"STT 프로세스가 비정상 종료되었습니다. exit={process.exitcode}") from None

            if status == "ok":
                return payload
            raise RuntimeError(f"STT 처리 실패:\n{payload}")
    finally:
        if process.is_alive():
            process.join(timeout=1)
        result_queue.close()
        result_queue.join_thread()


def _transcribe_cancellable(
    stop_event: Event,
    progress_callback: Optional[Callable[[str], None]],
    **kwargs,
) -> str:
    if stop_event.is_set():
        raise OperationCancelledError("STT 처리가 중단되었습니다.")

    wav = Path(kwargs["wav_path"])
    output = Path(kwargs["output_path"])
    progress_stop_event = threading.Event()
    progress_thread = _run_progress_loop(
        _get_expected_secs(kwargs["backend"], wav),
        progress_stop_event,
        progress_callback,
    )

    ctx = mp.get_context("spawn")
    attempts = [{"summary": kwargs["backend"], "retry_message": None, "kwargs": kwargs, "device": kwargs["backend"]}]
    if kwargs["backend"] == "qwen3_asr":
        attempts = _build_qwen_attempts(kwargs)

    try:
        last_error = None
        for attempt_index, attempt in enumerate(attempts):
            if kwargs["backend"] == "qwen3_asr":
                logger.info(
                    "Qwen3-ASR 시도 %d/%d: %s",
                    attempt_index + 1,
                    len(attempts),
                    attempt["summary"],
                )
                if attempt_index > 0 and progress_callback and attempt["retry_message"]:
                    progress_callback(f"[4/6] {attempt['retry_message']}")
            try:
                payload = _run_transcribe_attempt(
                    ctx=ctx,
                    stop_event=stop_event,
                    output=output,
                    result_kwargs=attempt["kwargs"],
                )
                if progress_callback:
                    progress_callback("[4/6] STT 처리 완료 (100%)")
                return payload
            except RuntimeError as exc:
                last_error = exc
                should_retry = (
                    kwargs["backend"] == "qwen3_asr"
                    and attempt["device"] == "mps"
                    and attempt_index + 1 < len(attempts)
                )
                if not should_retry:
                    raise
                logger.warning(
                    "Qwen3-ASR 시도 실패, 다음 런타임으로 재시도합니다: %s",
                    attempt["summary"],
                    exc_info=True,
                )
                if output.exists():
                    output.unlink(missing_ok=True)
                _clear_torch_cache()
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("STT 처리에 실패했습니다.")
    finally:
        progress_stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=1)


def transcribe(
    wav_path: str,
    output_path: str,
    original_filename: str,
    backend: str = DEFAULT_STT_BACKEND,
    model_name: str = DEFAULT_WHISPER_MODEL,
    quant: Optional[str] = DEFAULT_WHISPER_QUANT,
    batch_size: int = 4,
    language: str = "ko",
    progress_callback: Optional[Callable[[str], None]] = None,
    initial_prompt: Optional[str] = None,
    qwen_dtype: Optional[str] = None,
    qwen_device_map: Optional[str] = None,
    qwen_attn_implementation: Optional[str] = None,
    qwen_forced_aligner: Optional[str] = None,
    qwen_return_timestamps: bool = False,
    qwen_max_new_tokens: int = 4096,
    qwen_chunk_seconds: Optional[int] = None,
    stop_event: Optional[Event] = None,
) -> str:
    kwargs = {
        "wav_path": wav_path,
        "output_path": output_path,
        "original_filename": original_filename,
        "backend": backend,
        "model_name": model_name,
        "quant": quant,
        "batch_size": batch_size,
        "language": language,
        "initial_prompt": initial_prompt,
        "qwen_dtype": qwen_dtype,
        "qwen_device_map": qwen_device_map,
        "qwen_attn_implementation": qwen_attn_implementation,
        "qwen_forced_aligner": qwen_forced_aligner,
        "qwen_return_timestamps": qwen_return_timestamps,
        "qwen_max_new_tokens": qwen_max_new_tokens,
        "qwen_chunk_seconds": qwen_chunk_seconds,
    }
    if stop_event is not None:
        return _transcribe_cancellable(
            stop_event=stop_event,
            progress_callback=progress_callback,
            **kwargs,
        )
    return _transcribe_impl(
        progress_callback=progress_callback,
        **kwargs,
    )
