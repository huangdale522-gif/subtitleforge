"""API and domain models shared by the service and providers."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class MediaType(str, Enum):
    """Supported media types."""

    MOVIE = "movie"
    TV = "tv"


class SubtitleLanguage(str, Enum):
    """Canonical subtitle languages used by the service."""

    SIMPLIFIED = "zh-Hans"
    TRADITIONAL = "zh-Hant"


class SearchRequest(BaseModel):
    """Media context used to find subtitle candidates."""

    title: str = Field(min_length=1, max_length=512)
    original_title: str | None = Field(default=None, max_length=512)
    english_title: str | None = Field(default=None, max_length=512)
    year: int | None = Field(default=None, ge=1800, le=2200)
    imdb_id: str | None = Field(default=None, max_length=32)
    tmdb_id: int | None = Field(default=None, ge=1)
    media_type: MediaType = MediaType.MOVIE
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=0)
    languages: list[SubtitleLanguage] = Field(
        default_factory=lambda: [
            SubtitleLanguage.SIMPLIFIED,
            SubtitleLanguage.TRADITIONAL,
        ]
    )
    query: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def normalize_languages(self) -> SearchRequest:
        """Keep language order while removing duplicate values."""

        self.languages = list(dict.fromkeys(self.languages))
        return self


class ProcessRequest(SearchRequest):
    """Request to find and install subtitles beside a video file."""

    video_path: str = Field(min_length=1, max_length=4096)
    overwrite: bool = False
    dry_run: bool = False
    provider_failover: bool = False
    provider_order: list[str] | None = None

    @property
    def video(self) -> Path:
        """Return the video path as a path object."""

        return Path(self.video_path).expanduser()


class SubtitleCandidate(BaseModel):
    """Normalized candidate returned by a subtitle provider."""

    provider: str
    candidate_id: str
    title: str
    release_name: str | None = None
    language: SubtitleLanguage | None = None
    format: str = "unknown"
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    score: float = 0.0
    download_count: int = 0
    machine_translated: bool = False
    download_ref: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def display_name(self) -> str:
        """Return a compact human-readable candidate name."""

        return self.release_name or self.title or self.candidate_id


class DownloadedAsset(BaseModel):
    """Provider download result before it is installed."""

    content: bytes
    file_name: str = "subtitle.srt"


class DownloadedSubtitle(BaseModel):
    """A subtitle successfully written beside the video."""

    language: SubtitleLanguage
    provider: str
    candidate: str
    path: str
    format: str


class ProcessResult(BaseModel):
    """Result of one subtitle processing operation."""

    status: str
    video_path: str
    existing_languages: list[SubtitleLanguage] = Field(default_factory=list)
    downloaded: list[DownloadedSubtitle] = Field(default_factory=list)
    missing_languages: list[SubtitleLanguage] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False


class ScanRequest(BaseModel):
    """Configuration for a full inventory pass and a bounded download batch."""

    paths: list[str] = Field(default_factory=lambda: ["/media"])
    exclude_paths: list[str] = Field(default_factory=list)
    exclude_domestic_anime: bool = False
    domestic_anime_regions: list[str] = Field(
        default_factory=lambda: ["CN", "TW", "HK"]
    )
    domestic_anime_path_keywords: list[str] = Field(
        default_factory=lambda: [
            "国漫",
            "国产",
            "国产动漫",
            "国产动画",
            "国产番剧",
            "国创",
        ]
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: [
            ".mkv",
            ".mp4",
            ".avi",
            ".mov",
            ".wmv",
            ".m4v",
            ".ts",
            ".m2ts",
            ".webm",
        ]
    )
    languages: list[SubtitleLanguage] = Field(
        default_factory=lambda: [
            SubtitleLanguage.SIMPLIFIED,
            SubtitleLanguage.TRADITIONAL,
        ]
    )
    max_files: int = Field(default=20, ge=1, le=100)
    daily_download_limit: int = Field(default=10, ge=1, le=1000)
    overwrite: bool = False
    dry_run: bool = True
    retry_after_hours: int = Field(default=24, ge=1, le=24 * 365)
    provider_failover: bool = True
    provider_order: list[str] | None = None

    @model_validator(mode="after")
    def normalize_scan_lists(self) -> ScanRequest:
        """Normalize extensions and languages while preserving their order."""

        self.languages = list(dict.fromkeys(self.languages))
        self.domestic_anime_regions = list(
            dict.fromkeys(
                value.strip()
                for value in self.domestic_anime_regions
                if value.strip()
            )
        )
        self.domestic_anime_path_keywords = list(
            dict.fromkeys(
                value.strip()
                for value in self.domestic_anime_path_keywords
                if value.strip()
            )
        )
        self.video_extensions = list(
            dict.fromkeys(
                extension.strip().casefold()
                if extension.strip().startswith(".")
                else f".{extension.strip().casefold()}"
                for extension in self.video_extensions
                if extension.strip()
            )
        )
        return self


class ScanItemResult(BaseModel):
    """The processing outcome for one video discovered by a library scan."""

    video_path: str
    title: str
    year: int | None = None
    media_type: MediaType
    season: int | None = None
    episode: int | None = None
    status: str
    existing_languages: list[SubtitleLanguage] = Field(default_factory=list)
    missing_languages: list[SubtitleLanguage] = Field(default_factory=list)
    downloaded: list[DownloadedSubtitle] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ScanResult(BaseModel):
    """Full-inventory counts plus the bounded queue download result."""

    status: str
    discovered: int = 0
    checked: int = 0
    excluded_domestic_anime: int = 0
    existing: int = 0
    queued: int = 0
    queue_remaining: int = 0
    remaining: int = 0
    downloaded: int = 0
    downloads_today: int = 0
    daily_download_limit: int = 10
    download_limit: int = 0
    dry_run: bool = True
    items: list[ScanItemResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class JobRecord(BaseModel):
    """Persisted background job state."""

    job_id: str
    status: str
    created_at: str
    updated_at: str
    request: ProcessRequest
    result: ProcessResult | None = None
    error: str | None = None
