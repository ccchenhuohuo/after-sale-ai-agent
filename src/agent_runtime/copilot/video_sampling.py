from __future__ import annotations

import shutil
import subprocess
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


def _sample_dir(settings: Settings, asset_id: str, path: Path) -> Path:
    base_dir = Path(settings.support_video_sample_dir)
    if not base_dir.is_absolute():
        base_dir = ROOT / base_dir
    return base_dir / f"{_safe_part(asset_id)}_{short_hash(str(path))}"


def _safe_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    return safe[:120] or "video"
