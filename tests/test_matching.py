from subtitleforge.matching import candidate_allowed, rank_candidates
from subtitleforge.models import (
    MediaType,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)


def test_rank_prefers_exact_episode_and_human_subtitle() -> None:
    request = SearchRequest(
        title="Example Show",
        media_type=MediaType.TV,
        season=2,
        episode=4,
        languages=[SubtitleLanguage.SIMPLIFIED],
    )
    exact = SubtitleCandidate(
        provider="assrt",
        candidate_id="1",
        title="Example.Show.S02E04.1080p",
        language=SubtitleLanguage.SIMPLIFIED,
        format="srt",
        season=2,
        episode=4,
        score=4,
    )
    wrong_episode = SubtitleCandidate(
        provider="assrt",
        candidate_id="2",
        title="Example.Show.S02E03.1080p",
        language=SubtitleLanguage.SIMPLIFIED,
        format="srt",
        season=2,
        episode=3,
    )
    machine = SubtitleCandidate(
        provider="custom",
        candidate_id="3",
        title="Example.Show.S02E04.1080p",
        language=SubtitleLanguage.SIMPLIFIED,
        format="ass",
        season=2,
        episode=4,
        machine_translated=True,
    )
    assert candidate_allowed(exact, request)
    assert not candidate_allowed(wrong_episode, request)
    ranked = rank_candidates([machine, exact], request, ["assrt", "custom"], False)
    assert [item.candidate_id for item in ranked] == ["1"]


def test_unknown_language_can_fill_a_missing_language() -> None:
    request = SearchRequest(title="Movie", languages=[SubtitleLanguage.TRADITIONAL])
    candidate = SubtitleCandidate(
        provider="custom", candidate_id="x", title="Movie.2024"
    )
    assert candidate_allowed(candidate, request)


def test_exact_file_fingerprint_skips_title_similarity_filter() -> None:
    request = SearchRequest(title="本地化标题", year=2025)
    candidate = SubtitleCandidate(
        provider="xunlei",
        candidate_id="hash-match",
        title="Unrelated.Release.Name",
        download_ref={"exact_file_match": True},
    )
    assert candidate_allowed(candidate, request)
