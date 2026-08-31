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


PROBE_SAMPLE_RATE = 48000
PROBE_PULSE_DURATION = 0.016
# 3연타 패턴. 단발 클릭이면 초반 잡음/키보드 소리에 오검출되므로 간격 자체를 지문으로 쓴다.
PROBE_PULSES = (
    (0.000, 2200.0),
    (0.080, 2600.0),
    (0.160, 2200.0),
)
PROBE_TOTAL_SECONDS = 0.28


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
        sample_rate = PROBE_SAMPLE_RATE
        total_frames = int(sample_rate * PROBE_TOTAL_SECONDS)
        duration = PROBE_PULSE_DURATION
        with wave.open(str(self.probe_audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for frame_idx in range(total_frames):
                t = frame_idx / sample_rate
                sample = 0.0
                for start, freq in PROBE_PULSES:
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
                    "auto-meeting-note-v2 Sync Diagnostic Session",
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

    def record_probe_emission(self, **payload) -> None:
        with self._lock:
            self._metadata["probe"].update(_json_safe(payload))
            self._metadata["probe"]["emitted_at"] = _utc_now()
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


PROBE_LEAD_SECONDS = 0.45

# AVAudioPlayer 는 재생이 끝나기 전에 GC 되면 소리가 잘리므로 참조를 붙잡아 둔다.
_active_probe_players: list = []


def _prepare_flash_windows() -> list:
    """플래시 창을 alpha=0 으로 미리 띄워 둔다. 창 생성 비용(수십 ms)을 예약 시각 전에 털어내고,
    실제 발광은 alphaValue 변경 한 번으로 끝내기 위한 준비 단계."""
    from AppKit import NSBackingStoreBuffered, NSColor, NSScreen, NSWindow, NSWindowStyleMaskBorderless

    windows = []
    for screen in NSScreen.screens():
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen.frame(),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        window.setOpaque_(False)
        window.setAlphaValue_(0.0)
        window.setBackgroundColor_(NSColor.whiteColor())
        window.setIgnoresMouseEvents_(True)
        window.setLevel_(2000)
        window.orderFrontRegardless()
        windows.append(window)
    return windows


def _close_flash_windows_later(windows: list, duration_seconds: float) -> None:
    import rumps

    def _close(timer):
        timer.stop()
        for window in windows:
            window.orderOut_(None)

    rumps.Timer(_close, duration_seconds).start()


def _wait_until(target: float) -> None:
    remaining = target - time.time()
    if remaining > 0.02:
        time.sleep(remaining - 0.02)
    while time.time() < target:  # 마지막 20ms 는 스핀으로 맞춘다
        pass


def _schedule_probe_click(probe_audio_path: Path, lead_seconds: float) -> tuple[Optional[float], str]:
    """클릭을 lead_seconds 뒤에 예약 재생하고 실제 출력이 시작될 시각을 반환한다.

    afplay 를 그때그때 spawn 하면 프로세스 생성 + 오디오 디바이스 워밍업 지연(실측 0.5~0.9초,
    실행마다 편차 큼)이 타임스탬프에 그대로 섞여 들어가 진단이 "오디오가 1초 늦다"는 잘못된
    결론을 낸다. AVAudioPlayer.playAtTime: 은 디바이스 클럭 기준 예약이라 그 지연이 빠진다.
    """
    try:
        from AVFoundation import AVAudioPlayer
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(str(probe_audio_path))
        player, error = AVAudioPlayer.alloc().initWithContentsOfURL_error_(url, None)
        if player is None:
            raise RuntimeError(f"AVAudioPlayer 생성 실패: {error}")
        if not player.prepareToPlay():
            raise RuntimeError("AVAudioPlayer prepareToPlay 실패")

        wall_before = time.time()
        device_now = player.deviceCurrentTime()
        wall_after = time.time()
        if not player.playAtTime_(device_now + lead_seconds):
            raise RuntimeError("AVAudioPlayer playAtTime 실패")

        _active_probe_players[:] = [p for p in _active_probe_players if p.isPlaying()]
        _active_probe_players.append(player)
        return (wall_before + wall_after) / 2.0 + lead_seconds, "avaudioplayer_scheduled"
    except Exception as exc:
        logger.warning("클릭 예약 재생 실패, afplay 폴백 (타임스탬프 신뢰 불가): %s", exc)

    try:
        started_at = time.time()
        subprocess.Popen(
            ["/usr/bin/afplay", str(probe_audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return started_at, "afplay"
    except Exception as exc:
        logger.warning("진단 클릭 재생 실패: %s", exc)
        return None, "failed"


def emit_probe_signals(
    probe_audio_path: Path,
    include_flash: bool = True,
    duration_seconds: float = 0.18,
    lead_seconds: float = PROBE_LEAD_SECONDS,
) -> dict:
    """클릭과 화면 플래시를 같은 절대 시각에 내보내고 각 신호의 실제 발생 시각을 반환한다.

    메인 스레드를 lead_seconds 동안 붙잡으므로 싱크 진단 모드에서만 호출한다.
    """
    windows = []
    if include_flash:
        try:
            windows = _prepare_flash_windows()
        except Exception as exc:
            logger.warning("화면 플래시 준비 실패: %s", exc)

    click_started_at, click_method = _schedule_probe_click(probe_audio_path, lead_seconds)
    reliable = click_method == "avaudioplayer_scheduled"

    flash_started_at = None
    if windows:
        try:
            if reliable and click_started_at is not None:
                _wait_until(click_started_at)
            # windows[0] 은 메뉴바가 있는 주 디스플레이 = 녹화 대상 화면. 그 창만 먼저 켜고
            # 바로 시각을 찍는다. 창 전부를 켠 뒤 찍으면 창당 window server 왕복이 쌓여
            # 타임스탬프가 늦어진다(2화면 실측 72ms — 허용치 80ms 를 거의 다 먹는다).
            windows[0].setAlphaValue_(1.0)
            flash_started_at = time.time()
            for window in windows[1:]:
                window.setAlphaValue_(1.0)
            _close_flash_windows_later(windows, duration_seconds)
        except Exception as exc:
            logger.warning("화면 플래시 표시 실패: %s", exc)
            for window in windows:
                window.orderOut_(None)
            flash_started_at = None

    return {
        "include_flash": bool(include_flash),
        "flash_started_at": flash_started_at,
        "click_started_at": click_started_at,
        "click_method": click_method,
        "click_timing_reliable": reliable,
        "lead_seconds": lead_seconds,
    }


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


def detect_audio_onset(
    path: Path,
    expected_near: Optional[float] = None,
    search_radius: float = 1.0,
) -> Optional[float]:
    """프로브 클릭(3연타)이 시작되는 시각을 초 단위로 돌려준다. 못 찾으면 None.

    expected_near 를 주면 그 시각 ±search_radius 안에서만 찾는다. 마이크처럼 SNR 이 낮은
    트랙에서 다른 구간의 잡음을 클릭으로 오검출하지 않게 하는 용도로, 캡처 시작 시각 차이가
    1초를 넘지는 않는다는 물리적 제약을 쓴다.
    """
    try:
        envelope, sample_rate = _read_wav_envelope(path)
    except Exception as exc:
        logger.warning("오디오 onset 분석 실패 (%s): %s", path, exc)
        return None

    if not envelope:
        return None

    if max(envelope) < 1e-6:
        return None

    # 5ms 블록 평균으로 다운샘플
    step = max(sample_rate // 200, 1)
    blocks: list[float] = []
    for idx in range(0, len(envelope), step):
        window = envelope[idx:idx + step]
        if not window:
            break
        blocks.append(sum(window) / len(window))
    if not blocks:
        return None

    return _match_probe_pattern(blocks, step / sample_rate, expected_near, search_radius)


def _match_probe_pattern(
    blocks: list[float],
    block_seconds: float,
    expected_near: Optional[float] = None,
    search_radius: float = 1.0,
) -> Optional[float]:
    """프로브 3연타 간격을 정합 필터로 찾아 첫 펄스 시각을 돌려준다.

    단순 threshold 로는 마이크 트랙의 초반 잡음(키보드/책상 소리)을 클릭으로 오검출한다
    (실측: 진단 세션에서 실제 1.93초 클릭 대신 0.18초를 집어냄). 펄스 3개의 간격은
    주변 소음이 우연히 만들지 않는 지문이라 신호 대비 잡음이 낮은 마이크에서도 버틴다.
    """
    if not blocks:
        return None
    pulse_blocks = max(1, int(round(PROBE_PULSE_DURATION / block_seconds)))
    template = [0.0] * (int(round(PROBE_PULSES[-1][0] / block_seconds)) + pulse_blocks)
    for start, _freq in PROBE_PULSES:
        head = int(round(start / block_seconds))
        for offset in range(pulse_blocks):
            if head + offset < len(template):
                template[head + offset] = 1.0
    if len(blocks) < len(template) + 2:
        return None

    mean = sum(template) / len(template)
    template = [value - mean for value in template]

    scores = []
    for start in range(len(blocks) - len(template) + 1):
        scores.append(sum(t * blocks[start + idx] for idx, t in enumerate(template)))

    lower, upper = 0, len(scores) - 1
    if expected_near is not None:
        lower = max(0, int((expected_near - search_radius) / block_seconds))
        upper = min(len(scores) - 1, int((expected_near + search_radius) / block_seconds))
        if lower > upper:
            return None

    best_idx = max(range(lower, upper + 1), key=scores.__getitem__)
    best = scores[best_idx]
    if best <= 0:
        return None
    # 잡음 구간 대비 충분히 튀는지 확인 (패턴이 없는 파일에서 엉뚱한 위치를 고르지 않도록).
    # 잡음 기준은 찾은 패턴 자체를 제외한 구간에서, 탐색 창을 좁혀도 흔들리지 않게 전체에서 잡는다.
    outside = [score for idx, score in enumerate(scores) if abs(idx - best_idx) >= len(template)]
    if outside:
        noise = sorted(outside)[int(len(outside) * 0.9)]
        if noise > 0 and best < noise * 1.8:
            return None
    return best_idx * block_seconds


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


def _probe_emission_skew(probe: Optional[dict]) -> float:
    """클릭 발생 시각 - 플래시 발생 시각. 두 신호를 같은 시각에 쏘지 못한 만큼은
    싱크 오차가 아니라 프로브 자체의 편차이므로 측정값에서 빼 준다."""
    if not probe:
        return 0.0
    click_at = probe.get("click_started_at")
    flash_at = probe.get("flash_started_at")
    if isinstance(click_at, (int, float)) and isinstance(flash_at, (int, float)):
        return float(click_at) - float(flash_at)
    return 0.0


def infer_sync_cause(
    measurements: dict,
    tolerance: float = 0.08,
    probe: Optional[dict] = None,
    screen_snapshot: Optional[dict] = None,
) -> dict:
    raw_video = measurements.get("raw_video_flash")
    raw_system = measurements.get("raw_system_click")
    raw_mic = measurements.get("raw_mic_click")
    final_video = measurements.get("final_video_flash")
    final_mix = measurements.get("final_mixed_click")

    skew = _probe_emission_skew(probe)
    if probe is not None and probe.get("click_timing_reliable") is False:
        return {
            "category": "probe_timing_unreliable",
            "summary": (
                "클릭이 예약 재생(AVAudioPlayer)에 실패해 afplay 폴백으로 재생됐습니다. "
                "클릭 타임스탬프에 프로세스 spawn 지연이 섞여 있어 싱크 판정에 쓸 수 없습니다. 재측정하세요."
            ),
            "raw_mic_delta": None,
            "raw_system_delta": None,
            "final_delta": None,
        }

    # raw_*_delta 는 "오차"가 아니라 병합 때 적용해야 하는 offset(=필요량)이다.
    # raw 트랙은 원래 화면과 어긋나 있고(캡처 시작 시각이 다르니 당연하다) 그걸 offset 으로
    # 맞추는 것이 정상 동작이다. 실제 오차는 final_delta, 그리고 필요량과 적용량의 차이다.
    raw_system_delta = (raw_system - raw_video - skew) if raw_system is not None and raw_video is not None else None
    raw_mic_delta = (raw_mic - raw_video - skew) if raw_mic is not None and raw_video is not None else None
    final_delta = (final_mix - final_video - skew) if final_mix is not None and final_video is not None else None

    result = {
        "raw_mic_delta": raw_mic_delta,
        "raw_system_delta": raw_system_delta,
        "final_delta": final_delta,
    }

    applied_sys = (screen_snapshot or {}).get("sys_offset")
    applied_mic = (screen_snapshot or {}).get("mic_offset")
    sys_anchor_error = (
        float(applied_sys) - raw_system_delta
        if isinstance(applied_sys, (int, float)) and raw_system_delta is not None
        else None
    )
    mic_anchor_error = (
        float(applied_mic) - raw_mic_delta
        if isinstance(applied_mic, (int, float)) and raw_mic_delta is not None
        else None
    )
    result["sys_anchor_error"] = sys_anchor_error
    result["mic_anchor_error"] = mic_anchor_error

    # 마이크 경로만 어긋난 경우: 마이크가 요구하는 offset 이 시스템 트랙과 크게 다르다.
    # (둘 다 같은 화면을 기준으로 하니 요구량은 비슷해야 한다)
    if mic_anchor_error is not None and abs(mic_anchor_error) > tolerance and (
        sys_anchor_error is None or abs(sys_anchor_error) <= tolerance
    ):
        return {
            **result,
            "category": "mic_capture_or_mic_offset",
            "summary": (
                f"마이크에 적용된 offset 이 실측 필요량과 {mic_anchor_error * 1000:+.0f}ms 다릅니다. "
                "마이크 캡처 시작 시점 또는 mic offset 계산 경로가 원인입니다."
            ),
        }
    if mic_anchor_error is None and raw_mic_delta is not None and raw_system_delta is not None:
        mic_vs_sys = raw_mic_delta - raw_system_delta
        if abs(mic_vs_sys) > tolerance:
            return {
                **result,
                "category": "mic_capture_or_mic_offset",
                "summary": (
                    f"마이크 raw 트랙이 시스템 트랙과 {mic_vs_sys * 1000:+.0f}ms 어긋나 있습니다. "
                    "마이크 캡처 시작 시점 또는 mic offset 계산 경로가 원인입니다."
                ),
            }

    # 적용 offset 이 실측 필요량과 다르면 앵커(영상 t=0 / 오디오 t=0) 계산이 원인이다.
    if sys_anchor_error is not None and abs(sys_anchor_error) > tolerance:
        return {
            **result,
            "category": "capture_anchor",
            "summary": (
                f"적용된 sys_offset 이 실측 필요량과 {sys_anchor_error * 1000:+.0f}ms 다릅니다. "
                "영상 t=0 또는 오디오 t=0 앵커 계산이 원인입니다."
            ),
        }

    if final_delta is not None and abs(final_delta) <= tolerance:
        return {
            **result,
            "category": "in_sync",
            "summary": f"final 편차 {final_delta * 1000:+.0f}ms — 허용치({tolerance * 1000:.0f}ms) 안입니다.",
        }

    if final_delta is not None and abs(final_delta) > tolerance:
        if sys_anchor_error is not None and abs(sys_anchor_error) <= tolerance:
            return {
                **result,
                "category": "merge_or_mux",
                "summary": (
                    f"offset 계산은 맞는데(오차 {sys_anchor_error * 1000:+.0f}ms) final 이 "
                    f"{final_delta * 1000:+.0f}ms 어긋났습니다. ffmpeg 병합 또는 mux 단계가 원인입니다."
                ),
            }
        return {
            **result,
            "category": "merge_or_anchor",
            "summary": (
                f"final 이 {final_delta * 1000:+.0f}ms 어긋났습니다. 적용 offset 기록이 없어 "
                "앵커 계산과 병합 단계 중 어디가 원인인지 자동 분리할 수 없습니다."
            ),
        }

    return {
        **result,
        "category": "inconclusive",
        "summary": "자동 판정이 충분하지 않습니다. raw/final 측정치를 함께 보고 수동으로 확인해야 합니다.",
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

    # 영상 기준점은 실제로 측정된 플래시 프레임만 쓴다. 예전에는 플래시 측정이 없을 때
    # click_started_at - screen anchor 로 대체했지만, 그 값은 클릭 재생 지연을 그대로 물고 있어
    # 잘못된 mic_latency_correction 을 config 에 밀어 넣는 경로였다.
    video_probe_anchor = raw_video_flash

    if (
        probe.get("include_flash")
        and probe.get("click_timing_reliable") is not False
        and isinstance(video_probe_anchor, (int, float))
        and isinstance(raw_mic_click, (int, float))
        and isinstance(current_mic_offset, (int, float))
    ):
        desired_mic_offset = float(raw_mic_click) - float(video_probe_anchor) - _probe_emission_skew(probe)
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

    # 시스템 오디오는 탭에서 직접 받아 SNR 이 가장 높으므로 먼저 찾고,
    # 마이크는 그 근처에서만 찾는다 (캡처 시작 시각 차이는 1초를 넘지 않는다).
    raw_system_click = detect_audio_onset(raw_system) if raw_system else None
    measurements = {
        "raw_video_flash": detect_video_flash(raw_video) if raw_video else None,
        "raw_system_click": raw_system_click,
        "raw_mic_click": detect_audio_onset(raw_mic, expected_near=raw_system_click) if raw_mic else None,
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
        "inference": infer_sync_cause(
            measurements,
            probe=metadata.get("probe"),
            screen_snapshot=metadata.get("sync_snapshots", {}).get("screen_start"),
        ),
    }
    report["recommendations"] = recommend_sync_adjustments(
        report,
        fallback_current_mic_latency_correction=fallback_current_mic_latency_correction,
    )
    return report
