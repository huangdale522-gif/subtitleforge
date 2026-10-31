import io
import asyncio
import json
import zipfile
from pathlib import Path

from subtitleforge.models import DownloadedAsset, SubtitleLanguage
from subtitleforge.subtitle_files import (
    install_subtitle,
    language_from_name,
    probe_embedded_languages,
    scan_external_languages,
    select_subtitle,
)


def test_archive_selects_requested_language() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Movie.zh-tw.srt", "繁體中文字幕")
        archive.writestr("Movie.zh-cn.srt", "简体中文字幕")
    content, name = select_subtitle(
        DownloadedAsset(content=buffer.getvalue(), file_name="bundle.zip"),
        SubtitleLanguage.SIMPLIFIED,
        1024 * 1024,
    )
    assert name == "Movie.zh-cn.srt"
    assert "简体".encode() in content


def test_install_is_atomic_and_uses_media_friendly_suffix(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    destination, file_format = install_subtitle(
        video,
        b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        "downloaded.srt",
        SubtitleLanguage.SIMPLIFIED,
        False,
    )
    assert destination.name == "Movie.chi.zh-cn.srt"
    assert destination.read_bytes().startswith(b"1\n")
    assert file_format == "srt"
    assert language_from_name(destination.name) is SubtitleLanguage.SIMPLIFIED


def test_external_subtitle_must_match_video_stem(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Movie.2024.zh-cn.srt").write_text("简中", encoding="utf-8")
    assert scan_external_languages(video) == set()

    (tmp_path / "Movie.zh-cn.srt").write_text("简中", encoding="utf-8")
    assert scan_external_languages(video) == {SubtitleLanguage.SIMPLIFIED}


def test_external_subtitle_does_not_match_prefix_of_longer_video(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Movie.Extended.zh-cn.srt").write_text("简中", encoding="utf-8")
    assert scan_external_languages(video) == set()


def test_external_untagged_track_can_use_content_detection(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Movie.default.srt").write_text("这为中文", encoding="utf-8")
    assert scan_external_languages(video) == {SubtitleLanguage.SIMPLIFIED}


def test_embedded_generic_chinese_uses_track_title(monkeypatch) -> None:
    captured = []

    class FakeProcess:
        async def communicate(self):
            return json.dumps(
                {
                    "streams": [
                        {"tags": {"language": "zh", "title": "简体中文"}},
                        {"tags": {"language": "zh", "title": "繁體中文"}},
                    ]
                }
            ).encode(), b""

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    languages = asyncio.run(probe_embedded_languages(Path("Movie.mkv")))

    assert languages == {
        SubtitleLanguage.SIMPLIFIED,
        SubtitleLanguage.TRADITIONAL,
    }
    assert "stream_tags=language,title:stream_disposition=forced" in captured


def test_embedded_generic_chinese_simplified_title_does_not_hide_traditional(
    monkeypatch,
) -> None:
    class FakeProcess:
        async def communicate(self):
            return json.dumps(
                {"streams": [{"tags": {"language": "zh", "title": "简体中文"}}]}
            ).encode(), b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(probe_embedded_languages(Path("Movie.mkv"))) == {
        SubtitleLanguage.SIMPLIFIED
    }
