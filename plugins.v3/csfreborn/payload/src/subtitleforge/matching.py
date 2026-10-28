"""Candidate filtering and deterministic ranking."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .models import MediaType, SearchRequest, SubtitleCandidate, SubtitleLanguage

FORMAT_PREFERENCE = {"srt": 8.0, "ass": 7.0, "ssa": 6.0, "vtt": 5.0, "sub": 3.0}


def normalize_text(value: str) -> str:
    """Normalize titles for comparison without changing their meaning."""

    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text)
    return " ".join(text.split())


def _similarity(left: str, right: str) -> float:
    """Return a conservative title similarity score from 0 to 1."""

    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.92
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    else:
        overlap = 0.0
    return max(SequenceMatcher(None, left_norm, right_norm).ratio(), overlap)


def candidate_title_score(
    candidate: SubtitleCandidate, request: SearchRequest
) -> float:
    """Return the best title similarity across the candidate's names."""

    requested = [
        request.title,
        request.original_title or "",
        request.english_title or "",
        request.query or "",
    ]
    candidate_names = [candidate.title, candidate.release_name or ""]
    return max(
        (
            _similarity(left, right)
            for left in requested
            for right in candidate_names
            if left and right
        ),
        default=0.0,
    )


def candidate_allowed(candidate: SubtitleCandidate, request: SearchRequest) -> bool:
    """Apply hard language and episode constraints before ranking."""

    if candidate.language is not None and candidate.language not in request.languages:
        return False
    if request.media_type is MediaType.TV:
        if (
            candidate.season is not None
            and request.season is not None
            and candidate.season != request.season
        ):
            return False
        if (
            candidate.episode is not None
            and request.episode is not None
            and candidate.episode != request.episode
        ):
            return False
    if request.year and candidate.year and abs(candidate.year - request.year) > 1:
        return False
    if candidate.download_ref.get("exact_file_match"):
        # Hash providers bind the subtitle to this exact video, so a release
        # name that differs from the scraper title must not reject it.
        return True
    return candidate_title_score(candidate, request) >= 0.45


def rank_candidates(
    candidates: list[SubtitleCandidate],
    request: SearchRequest,
    provider_order: list[str],
    allow_machine_translated: bool,
) -> list[SubtitleCandidate]:
    """Filter candidates and sort the most likely matches first."""

    provider_rank = {name: index for index, name in enumerate(provider_order)}
    ranked: list[tuple[float, SubtitleCandidate]] = []
    for candidate in candidates:
        if not candidate_allowed(candidate, request):
            continue
        if candidate.machine_translated and not allow_machine_translated:
            continue
        score = candidate_title_score(candidate, request) * 100
        if candidate.language in request.languages:
            score += 35
        if request.media_type is MediaType.TV:
            if request.season is not None and candidate.season == request.season:
                score += 20
            if request.episode is not None and candidate.episode == request.episode:
                score += 30
        if request.year and candidate.year == request.year:
            score += 8
        score += FORMAT_PREFERENCE.get(candidate.format.lower(), 0)
        score += min(candidate.score, 10)
        score += min(candidate.download_count, 1000) / 200
        score += max(0, 10 - provider_rank.get(candidate.provider, 99))
        if candidate.machine_translated:
            score -= 25
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def candidates_for_language(
    candidates: list[SubtitleCandidate],
    language: SubtitleLanguage,
) -> list[SubtitleCandidate]:
    """Keep candidates known to match a language plus unknown-language files."""

    return [item for item in candidates if item.language in (None, language)]
