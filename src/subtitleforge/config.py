"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SubtitleForge settings.

    All settings use the ``SF_`` environment prefix. Secrets are intentionally
    kept in environment variables so they can be managed by a NAS secret store
    or Docker Compose without being written to the plugin database.
    """

    model_config = SettingsConfigDict(
        env_prefix="SF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_token: str = ""
    media_roots: str = "/media"
    data_dir: Path = Path("/data")
    provider_order: str = "xunlei,assrt,subtitle_best,opensubtitles,custom,shooter"
    subtitle_best_api_key: str = ""
    # Public client token used by the ChineseSubFinder upstream share-sub API.
    subtitle_best_header_token: str = "5akwmGAbuFWqgaZf9QwT"
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    assrt_token: str = ""
    xunlei_enabled: bool = True
    shooter_enabled: bool = False
    custom_provider_url: str = ""
    custom_provider_token: str = ""
    allow_machine_translated: bool = False
    request_timeout_seconds: float = 30.0
    max_download_mb: int = 80
    max_download_attempts_per_file: int = 3
    provider_cooldown_hours: int = 24
    provider_unavailable_cooldown_minutes: int = 15
    ffprobe_path: str = "ffprobe"

    @field_validator("data_dir", mode="before")
    @classmethod
    def expand_data_dir(cls, value: str | Path) -> Path:
        """Expand a leading ``~`` before the path is used."""

        return Path(value).expanduser()

    @property
    def media_root_paths(self) -> list[Path]:
        """Return configured media roots as resolved paths."""

        roots = [
            Path(item.strip()).expanduser()
            for item in self.media_roots.split(",")
            if item.strip()
        ]
        return [root.resolve() for root in roots]

    @property
    def provider_names(self) -> list[str]:
        """Return provider names in preference order."""

        names = [
            item.strip().lower()
            for item in self.provider_order.split(",")
            if item.strip()
        ]
        return list(dict.fromkeys(names))
