from __future__ import annotations

import shutil
import subprocess
import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.copilot.evidence import short_hash
from agent_runtime.settings import Settings


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class VideoSamplingResult:
    status: str
    frame_paths: list[str] = field(default_factory=list)
    model_name: str = "ffmpeg"
    error: str = ""


def sample_video_frames(video_path: str, asset_id: str, settings: Settings) -> VideoSamplingResult:
    if not settings.support_video_sampling_enabled:
        return VideoSamplingResult(status="unsupported", error="Video sampling disabled")
    if video_path.startswith(("http://", "https://")):
        return VideoSamplingResult(status="unsupported", error="Video sampling v1 requires a local video file")

    path = Path(video_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return VideoSamplingResult(status="unsupported", error="Video file does not exist")
    if not path.is_file():
        return VideoSamplingResult(status="unsupported", error="Video path is not a file")

    ffmpeg = settings.support_video_ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        return VideoSamplingResult(status="unsupported", error="ffmpeg not found")
    preflight = _preflight_video_file(path, settings, ffmpeg)
    if preflight is not None:
        return preflight

    output_dir = _sample_dir(settings, asset_id, path)
    output_dir.mkdir(parents=True, exist_ok=True)
    for existing in output_dir.glob("frame_*.jpg"):
        existing.unlink()

    frame_pattern = output_dir / "frame_%03d.jpg"
    frame_count = max(1, settings.support_video_sample_count)
    interval = max(0.5, settings.support_video_sample_interval_seconds)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-vf",
        f"fps=1/{interval},scale=720:-2:force_original_aspect_ratio=decrease",
        "-frames:v",
        str(frame_count),
        str(frame_pattern),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.support_video_sample_timeout_seconds,
        )
    except Exception as exc:
        return VideoSamplingResult(status="error", error=f"{type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
        return VideoSamplingResult(status="error", error=message[:500])

    frames = sorted(output_dir.glob("frame_*.jpg"))
    if not frames:
        return VideoSamplingResult(status="empty", error="ffmpeg produced no frames")
    return VideoSamplingResult(status="ok", frame_paths=[str(frame) for frame in frames])


def _preflight_video_file(path: Path, settings: Settings, ffmpeg: str) -> VideoSamplingResult | None:
    if not _looks_like_video_container(path):
        return VideoSamplingResult(status="unsupported", error="Video file signature is not a supported container")
    ffprobe = _ffprobe_path(ffmpeg)
    if not ffprobe:
        return VideoSamplingResult(status="unsupported", error="ffprobe not found")
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.support_video_probe_timeout_seconds,
        )
    except Exception as exc:
        return VideoSamplingResult(status="error", error=f"ffprobe {type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "ffprobe failed").strip()
        return VideoSamplingResult(status="unsupported", error=message[:500])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return VideoSamplingResult(status="unsupported", error="ffprobe returned invalid JSON")
    streams = payload.get("streams") if isinstance(payload, dict) else None
    video_streams = [stream for stream in streams or [] if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    if not video_streams:
        return VideoSamplingResult(status="unsupported", error="Video file contains no video stream")
    stream = video_streams[0]
    width = _int_value(stream.get("width"))
    height = _int_value(stream.get("height"))
    if width and settings.support_video_max_width and width > settings.support_video_max_width:
        return VideoSamplingResult(status="unsupported", error="Video width exceeds configured limit")
    if height and settings.support_video_max_height and height > settings.support_video_max_height:
        return VideoSamplingResult(status="unsupported", error="Video height exceeds configured limit")
    duration = _duration_seconds(stream.get("duration"))
    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    if duration <= 0:
        duration = _duration_seconds(format_payload.get("duration"))
    if duration and settings.support_video_max_duration_seconds and duration > settings.support_video_max_duration_seconds:
        return VideoSamplingResult(status="unsupported", error="Video duration exceeds configured limit")
    return None


def _looks_like_video_container(path: Path) -> bool:
    try:
        header = path.read_bytes()[:64]
    except OSError:
        return False
    if len(header) < 4:
        return False
    if header.startswith(b"\x1a\x45\xdf\xa3"):  # WebM / Matroska
        return True
    if header.startswith(b"RIFF") and b"AVI" in header[:16]:
        return True
    if header.startswith(b"OggS"):
        return True
    if b"ftyp" in header[4:16]:  # MP4 / MOV family
        return True
    return False


def _ffprobe_path(ffmpeg: str) -> str:
    configured = Path(ffmpeg)
    if configured.name == "ffmpeg":
        sibling = configured.with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or ""


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _duration_seconds(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sample_dir(settings: Settings, asset_id: str, path: Path) -> Path:
    base_dir = Path(settings.support_video_sample_dir)
    if not base_dir.is_absolute():
        base_dir = ROOT / base_dir
    return base_dir / f"{_safe_part(asset_id)}_{short_hash(str(path))}"


def _safe_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    return safe[:120] or "video"
