import json
import logging
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class SyncDiagnosticSession:
    def __init__(self, session_dir: Path, metadata: dict):
        self.session_dir = session_dir
        self.raw_dir = session_dir / "raw"
        self.final_dir = session_dir / "final"
        self.metadata_path = session_dir / "session.json"
        self.probe_audio_path = session_dir / "probe_click.wav"
        self._lock = threading.Lock()
        self._metadata = metadata

        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.final_dir.mkdir(parents=True, exist_ok=True)
        self._write_probe_audio()
        self._write_readme()
        self._persist()

    @classmethod
    def create(
        cls,
        output_dir: Path,
        mode: str,
        app_version: str,
        mic_enabled: bool,
    ) -> "SyncDiagnosticSession":
        base_dir = Path(output_dir).expanduser() / "_sync_diagnostics"
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_dir = base_dir / f"{session_id}_{mode}"
        metadata = {
            "session_id": session_id,
            "created_at": _utc_now(),
            "mode": mode,
            "app_version": app_version,
            "mic_enabled": bool(mic_enabled),
            "status": "recording",
            "artifacts": {},
            "sync_snapshots": {},
            "merge_stages": [],
            "probe": {
                "audio_asset": str(session_dir / "probe_click.wav"),
                "speaker_hint": "헤드폰 대신 스피커 출력으로 테스트해야 마이크 경로 진단이 가능합니다.",
            },
        }
        return cls(session_dir, metadata)

    def _persist(self) -> None:
        self.metadata_path.write_text(
            json.dumps(_json_safe(self._metadata), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_probe_audio(self) -> None:
        sample_rate = 48000
        total_frames = int(sample_rate * 0.28)
        pulses = (
            (0.000, 0.016, 2200.0),
            (0.080, 0.016, 2600.0),
            (0.160, 0.016, 2200.0),
        )
        with wave.open(str(self.probe_audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for frame_idx in range(total_frames):
                t = frame_idx / sample_rate
                sample = 0.0
                for start, duration, freq in pulses:
                    if start <= t < (start + duration):
                        ramp = min((t - start) / 0.002, 1.0, ((start + duration) - t) / 0.002)
                        sample += math.sin(2.0 * math.pi * freq * (t - start)) * ramp * 0.8
                clipped = max(-1.0, min(1.0, sample))
                wav_file.writeframes(struct.pack("<h", int(clipped * 32767)))

    def _write_readme(self) -> None:
        readme = self.session_dir / "README.txt"
        readme.write_text(
            "\n".join(
                [
                    "AutoMeetingNote Sync Diagnostic Session",
                    "",
                    "이 폴더에는 녹화 직후 보존된 raw/final 산출물과 싱크 메타데이터가 들어 있습니다.",
                    "마이크 경로를 진단하려면 테스트 중 헤드폰 대신 스피커 출력으로 재생하세요.",
                    "",
                    "주요 파일:",
                    "- session.json: 타임스탬프, offset, 병합 인자, 산출물 경로",
                    "- raw/: 병합 전에 보존한 원본 미디어",
                    "- final/: 병합 후 최종 결과물",
                    "- probe_click.wav: 진단용 클릭 패턴 원본",
                ]
            ),
            encoding="utf-8",
        )

    def record_runtime_context(self, **payload) -> None:
        with self._lock:
            self._metadata.setdefault("runtime", {}).update(_json_safe(payload))
            self._persist()

    def record_probe_emission(
        self,
        *,
        include_flash: bool,
        flash_started_at: Optional[float],
        click_started_at: Optional[float],
    ) -> None:
        with self._lock:
            self._metadata["probe"].update(
                {
                    "include_flash": bool(include_flash),
                    "flash_started_at": flash_started_at,
                    "click_started_at": click_started_at,
                    "emitted_at": _utc_now(),
                }
            )
            self._persist()

    def record_sync_snapshot(self, stage: str, payload: dict) -> None:
        with self._lock:
            self._metadata["sync_snapshots"][stage] = _json_safe(payload)
            self._persist()

    def record_merge_stage(
        self,
        stage: str,
        *,
        media_name: str,
        sys_offset: float,
        mic_offset: float,
        sys_args: Optional[list[str]],
        mic_args: Optional[list[str]],
    ) -> None:
        with self._lock:
            self._metadata["merge_stages"].append(
                {
                    "stage": stage,
                    "media_name": media_name,
                    "sys_offset": sys_offset,
                    "mic_offset": mic_offset,
                    "sys_args": _json_safe(sys_args),
                    "mic_args": _json_safe(mic_args),
                }
            )
            self._persist()

    def preserve_artifact(self, label: str, source_path: Optional[Path], group: str = "raw") -> Optional[Path]:
        if source_path is None:
            return None
        source = Path(source_path)
        if not source.exists():
            return None
        target_root = self.raw_dir if group == "raw" else self.final_dir
        target = target_root / source.name
        shutil.copy2(str(source), str(target))
        with self._lock:
            self._metadata["artifacts"][label] = {
                "group": group,
                "source": str(source),
                "copy": str(target),
                "size": target.stat().st_size,
            }
            self._persist()
        return target

    def finalize(self, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._metadata["status"] = status
            self._metadata["completed_at"] = _utc_now()
            if error:
                self._metadata["error"] = error
            self._persist()


def emit_screen_flash(duration_seconds: float = 0.18) -> Optional[float]:
    try:
        import rumps
        from AppKit import NSBackingStoreBuffered, NSColor, NSScreen, NSWindow, NSWindowStyleMaskBorderless

        windows = []
        for screen in NSScreen.screens():
            frame = screen.frame()
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            window.setOpaque_(True)
            window.setBackgroundColor_(NSColor.whiteColor())
            window.setIgnoresMouseEvents_(True)
            window.setLevel_(2000)
            window.orderFrontRegardless()
            windows.append(window)

        flashed_at = time.time()

        def _close(timer):
            timer.stop()
            for window in windows:
                window.orderOut_(None)

        rumps.Timer(_close, duration_seconds).start()
        return flashed_at
    except Exception as exc:
        logger.warning("화면 플래시 표시 실패: %s", exc)
        return None


def play_probe_click(probe_audio_path: Path) -> Optional[float]:
    try:
        started_at = time.time()
        subprocess.Popen(
            ["/usr/bin/afplay", str(probe_audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return started_at
    except Exception as exc:
        logger.warning("진단 클릭 재생 실패: %s", exc)
        return None


def _read_wav_envelope(path: Path, max_seconds: float = 8.0) -> tuple[list[float], int]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = min(wav_file.getnframes(), int(sample_rate * max_seconds))
            frames = wav_file.readframes(frame_count)
    except wave.Error as exc:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise exc
        with tempfile.NamedTemporaryFile(prefix="automeetingnote-wave-", suffix=".wav", delete=False) as tmp:
            converted = Path(tmp.name)
        try:
            subprocess.run(
                [
                    ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path),
                    "-t",
                    f"{max_seconds:.3f}",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    str(converted),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with wave.open(str(converted), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frame_count = min(wav_file.getnframes(), int(sample_rate * max_seconds))
                frames = wav_file.readframes(frame_count)
        finally:
            try:
                os.unlink(converted)
            except OSError:
                pass

    if sample_width == 2:
        values = struct.unpack("<" + "h" * (len(frames) // 2), frames)
        scale = 32768.0
    elif sample_width == 4:
        values = struct.unpack("<" + "f" * (len(frames) // 4), frames)
        scale = 1.0
    else:
        raise ValueError(f"지원하지 않는 WAV 샘플 폭: {sample_width}")

    envelope: list[float] = []
    for idx in range(0, len(values), channels):
        total = 0.0
        for ch in range(channels):
            sample = values[idx + ch]
            total += abs(float(sample) / scale)
        envelope.append(total / channels)
    return envelope, sample_rate


def detect_audio_onset(path: Path) -> Optional[float]:
    try:
        envelope, sample_rate = _read_wav_envelope(path)
    except Exception as exc:
        logger.warning("오디오 onset 분석 실패 (%s): %s", path, exc)
        return None

    if not envelope:
        return None

    peak = max(envelope)
    if peak < 1e-6:
        return None
    quiet_window = envelope[: min(len(envelope), max(sample_rate // 20, 1))]
    quiet_floor = min(quiet_window) if quiet_window else 0.0
    if quiet_floor <= 1e-6 and peak >= 0.02:
        return 0.0
    sorted_env = sorted(envelope)
    baseline = sorted_env[max(0, len(sorted_env) // 10)]
    peak = max(envelope)
    threshold = max(baseline * 6.0, peak * 0.20, 0.01)
    step = max(sample_rate // 200, 1)  # 5ms

    for idx in range(0, len(envelope), step):
        window = envelope[idx:idx + step]
        if not window:
            break
        level = sum(window) / len(window)
        if level >= threshold:
            return idx / sample_rate
    return None


def detect_video_flash(path: Path) -> Optional[float]:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None

    with tempfile.NamedTemporaryFile(prefix="automeetingnote-sync-", suffix=".txt", delete=False) as tmp:
        metadata_file = Path(tmp.name)

    try:
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"fps=120,scale=160:-1,signalstats,metadata=print:file={metadata_file}",
            "-frames:v",
            "720",
            "-f",
            "null",
            "-",
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pts_time = None
        samples: list[tuple[float, float]] = []
        for raw_line in metadata_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if line.startswith("frame:"):
                pts_time = None
                if "pts_time:" in line:
                    try:
                        pts_time = float(line.split("pts_time:", 1)[1].strip())
                    except ValueError:
                        pts_time = None
            elif line.startswith("pts_time:"):
                try:
                    pts_time = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pts_time = None
            elif "lavfi.signalstats.YAVG=" in line and pts_time is not None:
                try:
                    yavg = float(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                samples.append((pts_time, yavg))

        if not samples:
            return None

        baseline_values = [value for pts, value in samples if pts <= 0.5]
        if not baseline_values:
            baseline_values = [value for _, value in samples[:30]]
        baseline = sum(baseline_values) / len(baseline_values)
        threshold = baseline + 40.0
        for pts_time, yavg in samples:
            if yavg >= threshold:
                return pts_time
        return None
    except Exception as exc:
        logger.warning("비디오 플래시 분석 실패 (%s): %s", path, exc)
        return None
    finally:
        metadata_file.unlink(missing_ok=True)


def infer_sync_cause(measurements: dict, tolerance: float = 0.08) -> dict:
    raw_video = measurements.get("raw_video_flash")
    raw_system = measurements.get("raw_system_click")
    raw_mic = measurements.get("raw_mic_click")
    final_video = measurements.get("final_video_flash")
    final_mix = measurements.get("final_mixed_click")

    raw_system_delta = (raw_system - raw_video) if raw_system is not None and raw_video is not None else None
    raw_mic_delta = (raw_mic - raw_video) if raw_mic is not None and raw_video is not None else None
    final_delta = (final_mix - final_video) if final_mix is not None and final_video is not None else None

    if raw_mic_delta is not None and abs(raw_mic_delta) > tolerance and (
        raw_system_delta is None or abs(raw_system_delta) <= tolerance
    ):
        return {
            "category": "mic_capture_or_mic_offset",
            "summary": "마이크 raw 트랙만 화면 기준에서 벗어나 있으므로 마이크 캡처 시작 시점 또는 mic offset 계산 경로가 원인입니다.",
            "raw_mic_delta": raw_mic_delta,
            "raw_system_delta": raw_system_delta,
            "final_delta": final_delta,
        }

    if raw_system_delta is not None and raw_mic_delta is not None:
        if abs(raw_system_delta) > tolerance and abs(raw_mic_delta) > tolerance:
            if raw_system_delta * raw_mic_delta > 0:
                return {
                    "category": "video_anchor_or_probe_timing",
                    "summary": "시스템/마이크 raw 둘 다 같은 방향으로 벗어나 있어 화면 기준점 또는 진단 신호 시점 계산이 원인입니다.",
                    "raw_mic_delta": raw_mic_delta,
                    "raw_system_delta": raw_system_delta,
                    "final_delta": final_delta,
                }

    if final_delta is not None and abs(final_delta) > tolerance:
        if (raw_system_delta is None or abs(raw_system_delta) <= tolerance) and (
            raw_mic_delta is None or abs(raw_mic_delta) <= tolerance
        ):
            return {
                "category": "merge_or_mux",
                "summary": "raw 단계는 맞고 final만 어긋나 있으므로 ffmpeg 병합 또는 mux 단계가 원인입니다.",
                "raw_mic_delta": raw_mic_delta,
                "raw_system_delta": raw_system_delta,
                "final_delta": final_delta,
            }

    return {
        "category": "inconclusive",
        "summary": "자동 판정이 충분하지 않습니다. raw/final 측정치를 함께 보고 수동으로 확인해야 합니다.",
        "raw_mic_delta": raw_mic_delta,
        "raw_system_delta": raw_system_delta,
        "final_delta": final_delta,
    }


def recommend_sync_adjustments(report: dict, fallback_current_mic_latency_correction: Optional[float] = None) -> dict:
    session = report.get("session", {})
    measurements = report.get("measurements", {})
    probe = session.get("probe", {})
    screen_snapshot = session.get("sync_snapshots", {}).get("screen_start", {})
    runtime = session.get("runtime", {})

    recommendations: dict = {}

    raw_video_flash = measurements.get("raw_video_flash")
    raw_mic_click = measurements.get("raw_mic_click")
    current_mic_offset = screen_snapshot.get("mic_offset")
    current_applied_correction = runtime.get("mic_latency_correction_seconds")
    if not isinstance(current_applied_correction, (int, float)):
        current_applied_correction = fallback_current_mic_latency_correction
    if not isinstance(current_applied_correction, (int, float)):
        current_applied_correction = 0.0

    video_probe_anchor = raw_video_flash
    if not isinstance(video_probe_anchor, (int, float)):
        click_started_at = probe.get("click_started_at")
        screen_capture_started_at = screen_snapshot.get("screen.capture_started_at")
        screen_started_at = screen_snapshot.get("screen.started_at")
        if isinstance(click_started_at, (int, float)):
            if isinstance(screen_capture_started_at, (int, float)):
                video_probe_anchor = float(click_started_at) - float(screen_capture_started_at)
            elif isinstance(screen_started_at, (int, float)):
                video_probe_anchor = float(click_started_at) - float(screen_started_at)

    if (
        probe.get("include_flash")
        and isinstance(video_probe_anchor, (int, float))
        and isinstance(raw_mic_click, (int, float))
        and isinstance(current_mic_offset, (int, float))
    ):
        desired_mic_offset = float(raw_mic_click) - float(video_probe_anchor)
        correction = float(current_applied_correction) + (float(current_mic_offset) - desired_mic_offset)
        if 0.05 <= abs(correction) <= 2.0:
            recommendations["mic_latency_correction_seconds"] = round(correction, 3)

    return recommendations


def analyze_session(session_dir: Path, fallback_current_mic_latency_correction: Optional[float] = None) -> dict:
    session_dir = Path(session_dir)
    metadata_path = session_dir / "session.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"session.json이 없습니다: {metadata_path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    artifacts = metadata.get("artifacts", {})

    def _artifact_copy(name: str) -> Optional[Path]:
        copy_path = artifacts.get(name, {}).get("copy")
        return Path(copy_path) if copy_path else None

    raw_video = _artifact_copy("raw_video")
    raw_system = _artifact_copy("raw_system_audio")
    raw_mic = _artifact_copy("raw_mic_audio")
    final_video = _artifact_copy("final_video")
    final_audio = _artifact_copy("final_audio")

    measurements = {
        "raw_video_flash": detect_video_flash(raw_video) if raw_video else None,
        "raw_system_click": detect_audio_onset(raw_system) if raw_system else None,
        "raw_mic_click": detect_audio_onset(raw_mic) if raw_mic else None,
        "final_video_flash": detect_video_flash(final_video) if final_video else None,
        "final_mixed_click": detect_audio_onset(final_audio) if final_audio else None,
    }

    if final_video and measurements["final_mixed_click"] is None:
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            with tempfile.NamedTemporaryFile(prefix="automeetingnote-final-audio-", suffix=".wav", delete=False) as tmp:
                extracted_audio = Path(tmp.name)
            try:
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(final_video),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "48000",
                        "-y",
                        str(extracted_audio),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                measurements["final_mixed_click"] = detect_audio_onset(extracted_audio)
            except Exception as exc:
                logger.warning("최종 비디오 오디오 추출 실패: %s", exc)
            finally:
                extracted_audio.unlink(missing_ok=True)

    report = {
        "session": metadata,
        "measurements": measurements,
        "inference": infer_sync_cause(measurements),
    }
    report["recommendations"] = recommend_sync_adjustments(
        report,
        fallback_current_mic_latency_correction=fallback_current_mic_latency_correction,
    )
    return report
