from pathlib import Path
from types import SimpleNamespace

import agent_runtime.copilot.video_sampling as video_sampling
from agent_runtime.copilot.video_sampling import sample_video_frames
from agent_runtime.settings import Settings


def test_sample_video_frames_invokes_ffmpeg_and_returns_frame_paths(monkeypatch, tmp_path):
    video = tmp_path / "fault.mp4"
    video.write_bytes(b"fake-video")
    captured = {}

    def fake_run(command, check, capture_output, text, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        output_pattern = Path(command[-1])
        (output_pattern.parent / "frame_001.jpg").write_bytes(b"fake-frame")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_sampling.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_sampling.subprocess, "run", fake_run)
    settings = Settings(
        support_video_sample_dir=str(tmp_path / "samples"),
        support_video_sample_count=2,
        support_video_sample_interval_seconds=4,
        support_video_sample_timeout_seconds=9,
    )

    result = sample_video_frames(str(video), "video_fault", settings)

    assert result.status == "ok"
    sample_dir = tmp_path / "samples" / f"video_fault_{video_sampling.short_hash(str(video))}"
    assert result.frame_paths == [str(sample_dir / "frame_001.jpg")]
    assert captured["command"][0] == "/usr/bin/ffmpeg"
    assert captured["command"][captured["command"].index("-frames:v") + 1] == "2"
    assert "fps=1/4.0" in captured["command"][captured["command"].index("-vf") + 1]
    assert captured["timeout"] == 9


def test_sample_video_frames_without_ffmpeg_is_unsupported(monkeypatch, tmp_path):
    video = tmp_path / "fault.mp4"
    video.write_bytes(b"fake-video")
    monkeypatch.setattr(video_sampling.shutil, "which", lambda name: None)

    result = sample_video_frames(str(video), "video_fault", Settings())

    assert result.status == "unsupported"
    assert result.error == "ffmpeg not found"
