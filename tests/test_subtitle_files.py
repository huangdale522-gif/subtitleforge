import io
import zipfile
from pathlib import Path

from subtitleforge.models import DownloadedAsset, SubtitleLanguage
from subtitleforge.subtitle_files import (
    install_subtitle,
    language_from_name,
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
