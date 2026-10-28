"""Application service that coordinates providers, matching and installation."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .matching import candidates_for_language, rank_candidates
from .models import (
    DownloadedSubtitle,
    ProcessRequest,
    ProcessResult,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from .providers import (
    AssrtProvider,
    CustomHttpProvider,
    OpenSubtitlesProvider,
    ProviderError,
    ProviderQuotaError,
    ShooterProvider,
    SubtitleBestProvider,
    SubtitleProvider,
    XunleiProvider,
)
from .subtitle_files import (
    ensure_path_allowed,
    install_subtitle,
    language_from_content,
    probe_embedded_languages,
    scan_external_languages,
    select_subtitle,
)


class SubtitleService:
    """Find and install subtitles for media files."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "SubtitleForge/0.3.1"},
        )
        self.providers: dict[str, SubtitleProvider] = {
            "xunlei": XunleiProvider(
                self._client,
                settings.xunlei_enabled,
            ),
            "subtitle_best": SubtitleBestProvider(
                self._client,
                settings.subtitle_best_api_key,
                settings.subtitle_best_header_token,
            ),
            "opensubtitles": OpenSubtitlesProvider(
                self._client,
                settings.opensubtitles_api_key,
                settings.opensubtitles_username,
                settings.opensubtitles_password,
            ),
            "assrt": AssrtProvider(self._client, settings.assrt_token),
            "shooter": ShooterProvider(
                self._client,
                settings.shooter_enabled,
            ),
            "custom": CustomHttpProvider(
                self._client,
                settings.custom_provider_url,
                settings.custom_provider_token,
            ),
        }
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._provider_cooldowns: dict[str, float] = {}
        self._load_provider_cooldowns()

    async def close(self) -> None:
        """Release provider and HTTP resources."""

        for provider in self.providers.values():
            await provider.close()
        if self._owns_client:
            await self._client.aclose()

    def _provider_order(self, requested: list[str] | None = None) -> list[str]:
        """Resolve provider preference order."""

        names = requested or self.settings.provider_names
        return [name for name in names if name in self.providers]

    def _provider_in_cooldown(self, name: str) -> bool:
        """Return whether a provider is cooling down after a quota error."""

        deadline = self._provider_cooldowns.get(name)
        if deadline is None:
            return False
        if deadline <= time.time():
            self._provider_cooldowns.pop(name, None)
            self._save_provider_cooldowns()
            return False
        return True

    def _activate_provider_cooldown(self, name: str) -> None:
        """Stop spending requests on a provider until its cooldown expires."""

        cooldown_seconds = max(0, self.settings.provider_cooldown_hours) * 3600
        self._provider_cooldowns[name] = time.time() + cooldown_seconds
        self._save_provider_cooldowns()

    @property
    def _provider_cooldown_file(self) -> Path:
        """Return the small local state file used for quota cooldowns."""

        return self.settings.data_dir / "provider_cooldowns.json"

    def _load_provider_cooldowns(self) -> None:
        """Restore unexpired provider cooldowns after a service restart."""

        path = self._provider_cooldown_file
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        now = time.time()
        self._provider_cooldowns = {
            str(name): float(deadline)
            for name, deadline in payload.items()
            if isinstance(deadline, (int, float)) and float(deadline) > now
        }

    def _save_provider_cooldowns(self) -> None:
        """Persist quota cooldowns without exposing provider credentials."""

        path = self._provider_cooldown_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._provider_cooldowns, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            # Quota protection still works for this process if persistence fails.
            pass

    def status(self) -> dict[str, Any]:
        """Return public provider configuration and cooldown state."""

        order = self._provider_order()
        items: list[dict[str, Any]] = []
        for name in order:
            provider = self.providers[name]
            cooling_down = self._provider_in_cooldown(name)
            deadline = self._provider_cooldowns.get(name)
            items.append(
                {
                    "name": name,
                    "configured": provider.configured,
                    "cooling_down": cooling_down,
                    "cooldown_until": (
                        datetime.fromtimestamp(deadline, UTC).isoformat()
                        if cooling_down and deadline is not None
                        else None
                    ),
                }
            )
        return {
            "order": order,
            "cooldown_hours": max(0, self.settings.provider_cooldown_hours),
            "items": items,
        }

    async def search(
        self,
        request: SearchRequest,
        provider_order: list[str] | None = None,
        provider_failover: bool = False,
    ) -> tuple[list[SubtitleCandidate], list[str]]:
        """Search configured providers and return ranked candidates."""

        candidates: list[SubtitleCandidate] = []
        errors: list[str] = []
        order = self._provider_order(provider_order)
        for name in order:
            provider = self.providers[name]
            if not provider.configured or self._provider_in_cooldown(name):
                continue
            try:
                provider_candidates = await provider.search(request)
                candidates.extend(provider_candidates)
                if provider_failover and provider_candidates:
                    break
            except ProviderQuotaError as exc:
                errors.append(f"{name}: {exc}; cooling down")
                self._activate_provider_cooldown(name)
            except ProviderError as exc:
                errors.append(f"{name}: {exc}")
        ranked = rank_candidates(
            candidates,
            request,
            order,
            self.settings.allow_machine_translated,
        )
        return ranked, errors

    async def process(self, request: ProcessRequest) -> ProcessResult:
        """Find missing languages and install the best matching subtitle."""

        roots = self.settings.media_root_paths
        video = ensure_path_allowed(request.video, roots)
        if not video.is_file():
            raise ValueError("Video file does not exist")
        lock_key = str(video)
        async with self._locks_guard:
            lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._process_locked(request, video)

    async def _process_locked(
        self, request: ProcessRequest, video: Path
    ) -> ProcessResult:
        """Perform one serialized processing operation."""

        existing = scan_external_languages(video)
        if not set(request.languages).issubset(existing):
            existing.update(
                await probe_embedded_languages(video, self.settings.ffprobe_path)
            )
        missing = (
            list(request.languages)
            if request.overwrite
            else [
                language for language in request.languages if language not in existing
            ]
        )
        result = ProcessResult(
            status="skipped" if not missing else "processing",
            video_path=str(video),
            existing_languages=sorted(existing, key=lambda item: item.value),
            missing_languages=missing,
            dry_run=request.dry_run,
        )
        if not missing:
            return result

        if request.dry_run:
            result.status = "dry_run"
            return result

        candidates, errors = await self.search(
            request,
            request.provider_order,
            request.provider_failover,
        )
        result.errors.extend(errors)
        if not candidates:
            result.status = "not_found"
            return result

        unresolved: list[SubtitleLanguage] = []
        for language in missing:
            language_candidates = candidates_for_language(candidates, language)
            installed = False
            attempts = max(1, self.settings.max_download_attempts_per_file)
            for candidate in language_candidates[:attempts]:
                provider = self.providers.get(candidate.provider)
                if provider is None or not provider.configured:
                    continue
                if self._provider_in_cooldown(candidate.provider):
                    continue
                try:
                    asset = await provider.download(candidate)
                    if len(asset.content) > self.settings.max_download_mb * 1024 * 1024:
                        raise ValueError(
                            "Downloaded subtitle exceeds the configured size limit"
                        )
                    content, file_name = select_subtitle(
                        asset,
                        language,
                        self.settings.max_download_mb * 1024 * 1024,
                    )
                    detected = language_from_content(content)
                    if detected is not None and detected is not language:
                        raise ValueError(
                            f"Detected {detected.value} instead of {language.value}"
                        )
                    destination, file_format = install_subtitle(
                        video,
                        content,
                        file_name,
                        language,
                        request.overwrite,
                    )
                    result.downloaded.append(
                        DownloadedSubtitle(
                            language=language,
                            provider=candidate.provider,
                            candidate=candidate.display_name,
                            path=str(destination),
                            format=file_format,
                        )
                    )
                    installed = True
                    break
                except ProviderQuotaError as exc:
                    result.errors.append(
                        f"{candidate.provider}/{candidate.candidate_id}: {exc}"
                    )
                    self._activate_provider_cooldown(candidate.provider)
                    break
                except (ProviderError, ValueError, FileExistsError, OSError) as exc:
                    result.errors.append(
                        f"{candidate.provider}/{candidate.candidate_id}: {exc}"
                    )
            if not installed:
                unresolved.append(language)

        result.missing_languages = unresolved
        result.status = (
            "completed"
            if not unresolved
            else ("partial" if result.downloaded else "not_found")
        )
        return result
