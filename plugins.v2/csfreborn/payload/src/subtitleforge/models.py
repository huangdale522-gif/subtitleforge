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


class JobRecord(BaseModel):
    """Persisted background job state."""

    job_id: str
    status: str
    created_at: str
    updated_at: str
    request: ProcessRequest
    result: ProcessResult | None = None
    error: str | None = None
