from agent_runtime.copilot.asset_inputs import validate_support_asset_input
from agent_runtime.copilot.case_context import SupportAsset
from agent_runtime.settings import Settings


def test_local_asset_input_must_be_inside_allowed_dirs(tmp_path):
    allowed = tmp_path / "assets"
    allowed.mkdir()
    image = allowed / "damage.jpg"
    image.write_bytes(b"fake-image")

    settings = Settings(support_asset_allowed_local_dirs=str(allowed))
    result = validate_support_asset_input(
        SupportAsset(asset_id="img_1", media_type="image", local_path=str(image)),
        settings,
        expected_kind="image",
    )

    assert result.ok is True
    assert result.source_kind == "local_file"
    assert result.value == str(image.resolve())


def test_local_asset_input_rejects_path_escape_and_oversized_file(tmp_path):
    allowed = tmp_path / "assets"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"fake-image")
    large = allowed / "large.jpg"
    large.write_bytes(b"123456")
    settings = Settings(
        support_asset_allowed_local_dirs=str(allowed),
        support_asset_input_max_bytes=5,
    )

    escaped = validate_support_asset_input(
        SupportAsset(asset_id="img_1", media_type="image", local_path=str(outside)),
        settings,
        expected_kind="image",
    )
    oversized = validate_support_asset_input(
        SupportAsset(asset_id="img_2", media_type="image", local_path=str(large)),
        settings,
        expected_kind="image",
    )

    assert escaped.ok is False
    assert "允许的缓存目录" in escaped.error
    assert oversized.ok is False
    assert "超过允许大小" in oversized.error


def test_local_asset_input_rejects_mime_mismatch(tmp_path):
    allowed = tmp_path / "assets"
    allowed.mkdir()
    file_path = allowed / "notes.txt"
    file_path.write_text("not an image", encoding="utf-8")
    settings = Settings(support_asset_allowed_local_dirs=str(allowed))

    result = validate_support_asset_input(
        SupportAsset(asset_id="file_1", media_type="image", local_path=str(file_path)),
        settings,
        expected_kind="image",
    )

    assert result.ok is False
    assert "不是可处理的 image 类型" in result.error


def test_url_asset_input_requires_https_whitelisted_public_host():
    settings = Settings(support_asset_allowed_url_hosts="assets.example.test,*.cdn.example.test")

    assert (
        validate_support_asset_input(
            SupportAsset(asset_id="img_ok", media_type="image", url="https://assets.example.test/a.png"),
            settings,
            expected_kind="image",
        ).ok
        is True
    )
    assert (
        validate_support_asset_input(
            SupportAsset(asset_id="img_ok_sub", media_type="image", url="https://img.cdn.example.test/a.png"),
            settings,
            expected_kind="image",
        ).ok
        is True
    )
    for url in [
        "http://assets.example.test/a.png",
        "https://localhost/a.png",
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest/meta-data",
        "https://unlisted.example.test/a.png",
    ]:
        result = validate_support_asset_input(
            SupportAsset(asset_id="img_bad", media_type="image", url=url),
            settings,
            expected_kind="image",
        )
        assert result.ok is False, url


def test_video_url_is_unsupported_for_local_only_sampling():
    result = validate_support_asset_input(
        SupportAsset(asset_id="video_1", media_type="video", url="https://assets.example.test/fault.mp4"),
        Settings(support_asset_allowed_url_hosts="assets.example.test"),
        expected_kind="video",
        allow_url=False,
        local_only=True,
    )

    assert result.ok is False
    assert "远程 URL 暂不支持" in result.error
