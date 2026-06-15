from pathlib import Path

import agent_runtime.copilot.video_sampling as video_sampling
from agent_runtime.copilot.video_sampling import sample_video_frames
from agent_runtime.settings import Settings


def test_video_sampling_rejects_spoofed_mp4_before_ffprobe(tmp_path):
    fake_video = tmp_path / "fault.mp4"
    fake_video.write_text("not a video", encoding="utf-8")

    result = sample_video_frames(
        str(fake_video),
        "video_asset",
        Settings(support_video_ffmpeg_path="/bin/echo"),
    )

    assert result.status == "unsupported"
    assert "signature" in result.error


def test_video_sampling_ffprobe_preflight_requires_video_stream(monkeypatch, tmp_path):
    video = tmp_path / "fault.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42smoke")
    commands = []

    def fake_which(name):
        if name == "ffprobe":
            return "/usr/bin/ffprobe"
        return ""

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            returncode = 0
            stdout = '{"streams":[{"codec_type":"audio"}],"format":{"duration":"10"}}'
            stderr = ""

        return Completed()

    monkeypatch.setattr(video_sampling.shutil, "which", fake_which)
    monkeypatch.setattr(video_sampling.subprocess, "run", fake_run)

    result = sample_video_frames(
        str(video),
        "video_asset",
        Settings(support_video_ffmpeg_path="/usr/bin/ffmpeg"),
    )

    assert result.status == "unsupported"
    assert "no video stream" in result.error
    assert len(commands) == 1
    assert Path(commands[0][0]).name == "ffprobe"
