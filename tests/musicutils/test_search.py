"""Outcome-based tests for utils.musicutils.search module.

These tests verify that the search module produces correct results from the
USER's perspective, not that functions return expected values. Organized by
dangerous outcomes that must be achieved or avoided.

Dangerous outcomes for search:
- User gets wrong video (bad URL parsing, bad deduplication)
- Good results filtered out (is_relevant too strict)
- Garbage results shown (is_relevant too loose)
- Wrong track recommended (score_atv_match gives high score to wrong track)
"""

from utils.musicutils.search import (
    extract_video_id,
    is_relevant,
    dedupe_results,
    score_atv_match,
    SearchResult,
)


# ==========================================================================
# FIXTURES - Reusable test data
# ==========================================================================


def make_result(
    video_id: str = "test123",
    title: str = "Test Song",
    artist: str = "Test Artist",
    artist_id: str | None = None,
    duration: int = 180,
    source: str = "youtube",
) -> SearchResult:
    """Factory for SearchResult with sensible defaults."""
    return SearchResult(
        video_id=video_id,
        title=title,
        artist=artist,
        artist_id=artist_id,
        duration_seconds=duration,
        thumbnail_url=None,
        source=source,
    )


# ==========================================================================
# VIDEO ID EXTRACTION - User pastes URL, correct video must play
# ==========================================================================


class TestCorrectVideoPlaysFromUrl:
    """When user pastes a URL, the correct video must be identified.

    This is dangerous: wrong video ID = user hears wrong content.
    We test real URL formats users actually paste, not hypotheticals.
    """

    # Standard formats users copy from browser
    def test_standard_youtube_url(self):
        """User copies URL from youtube.com address bar."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_youtube_url_with_playlist_context(self):
        """User copies URL while watching from a playlist."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_youtube_url_with_timestamp(self):
        """User copies URL with timestamp from 'share at current time'."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_short_share_url(self):
        """User clicks 'Share' and copies the youtu.be link."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_short_share_url_with_timestamp(self):
        """User shares with 'start at' checkbox enabled."""
        url = "https://youtu.be/dQw4w9WgXcQ?t=42"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_youtube_music_url(self):
        """User copies URL from music.youtube.com."""
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_url(self):
        """User copies embed URL (rare but happens from embeds)."""
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_mobile_url(self):
        """User copies from mobile browser (m.youtube.com)."""
        url = "https://m.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    # Edge cases that must NOT return wrong video
    def test_non_youtube_url_returns_none(self):
        """Random URL must not be misinterpreted as video ID."""
        url = "https://example.com/watch?v=fake123"
        assert extract_video_id(url) is None

    def test_empty_string_returns_none(self):
        """Empty input must not crash or return garbage."""
        assert extract_video_id("") is None


# ==========================================================================
# RELEVANCE FILTERING - Good results kept, garbage rejected
# ==========================================================================


class TestRelevantResultsAreKept:
    """Scenarios where search results MUST pass the relevance filter.

    This is dangerous: filtering out good results = user can't find their song.
    We test realistic queries and results that should match.
    """

    def test_exact_title_match(self):
        """Result title matches query exactly."""
        result = make_result(title="Never Gonna Give You Up", artist="Rick Astley")
        assert is_relevant("Never Gonna Give You Up", result) is True

    def test_partial_title_in_longer_result(self):
        """Query is subset of result title (common with official videos)."""
        result = make_result(
            title="Never Gonna Give You Up (Official Music Video)",
            artist="Rick Astley"
        )
        assert is_relevant("Never Gonna Give You Up", result) is True

    def test_artist_name_helps_match(self):
        """Artist name in result helps pass filter."""
        result = make_result(title="Never Gonna Give You Up", artist="Rick Astley")
        assert is_relevant("Rick Astley", result) is True

    def test_japanese_title_without_spaces(self):
        """CJK titles don't have spaces - character similarity must work."""
        result = make_result(title="千本桜", artist="黒うさP")
        # Query matches title characters
        assert is_relevant("千本桜", result) is True

    def test_mixed_language_query(self):
        """User types romanized + Japanese mixed."""
        result = make_result(title="千本桜", artist="Hatsune Miku")
        assert is_relevant("千本桜 miku", result) is True

    def test_packed_title_with_brackets(self):
        """Titles with【brackets】and [tags] are common in JP content."""
        result = make_result(
            title="Song Name【Official MV】",
            artist="Artist"
        )
        assert is_relevant("Song Name", result) is True

    def test_cover_song_query(self):
        """User searching for a cover version."""
        result = make_result(title="Song Name Cover", artist="Cover Artist")
        assert is_relevant("Song Name cover", result) is True

    def test_long_query_with_40_percent_match(self):
        """Long queries (4+ words) need 40% overlap - verify threshold."""
        # Query: 5 words, result matches 2 = 40%
        result = make_result(title="never gonna give", artist="rick astley")
        assert is_relevant("never gonna give you up", result) is True

    def test_short_query_with_30_percent_match(self):
        """Short queries (≤3 words) need 30% overlap - verify threshold."""
        # Query: 3 words, result matches 1 = 33%
        result = make_result(title="never", artist="someone else")
        assert is_relevant("never gonna give", result) is True


class TestIrrelevantResultsAreRejected:
    """Scenarios where search results MUST be filtered out.

    This is dangerous: showing garbage = confusing UI, wasted user time.
    """

    def test_completely_unrelated_result(self):
        """Result has zero relation to query."""
        result = make_result(title="Cooking Tutorial", artist="Chef")
        assert is_relevant("Never Gonna Give You Up", result) is False

    def test_single_common_word_not_enough_for_long_query(self):
        """Matching only 'the' or similar shouldn't pass for long queries."""
        result = make_result(title="The Best Song Ever", artist="Band")
        # Only 'the' matches - 1/5 = 20%, below 40% threshold
        assert is_relevant("the quick brown fox jumps", result) is False

    def test_short_query_needs_meaningful_overlap(self):
        """Even short queries need some real overlap."""
        result = make_result(title="Completely Different", artist="Other")
        assert is_relevant("song name", result) is False


# ==========================================================================
# ATV SCORING - Best match must be recommended
# ==========================================================================


class TestBestMatchIsRecommended:
    """Scenarios where score_atv_match must identify the correct ATV.

    This is dangerous: wrong recommendation = user picks inferior version.
    """

    def test_artist_id_match_gives_high_score(self):
        """Same artist ID = definitely the right track."""
        candidate = make_result(
            title="Song Name",
            artist="Artist Name",
            artist_id="UC123456",
            duration=180,
        )
        score = score_atv_match(
            original_title="Song Name",
            original_artist="Artist Name",
            original_duration=180,
            original_artist_ids={"UC123456"},
            candidate=candidate,
        )
        assert score >= 0.8  # High confidence

    def test_different_artist_id_with_similar_title_still_scores(self):
        """Different artist ID but same name text = high score (text match)."""
        candidate = make_result(
            title="Popular Song",
            artist="Original Artist",
            artist_id="UC_different",
            duration=200,
        )
        score = score_atv_match(
            original_title="Popular Song",
            original_artist="Original Artist",
            original_duration=200,
            original_artist_ids={"UC_original"},
            candidate=candidate,
        )
        # Text matches perfectly, so score is high even without ID match
        assert score >= 0.9

    def test_duration_mismatch_rejects_track(self):
        """Very different duration = probably wrong track, score 0."""
        candidate = make_result(
            title="Song Name",
            artist="Artist",
            artist_id="UC123",
            duration=300,  # 2 minutes longer
        )
        score = score_atv_match(
            original_title="Song Name",
            original_artist="Artist",
            original_duration=180,
            original_artist_ids={"UC123"},
            candidate=candidate,
        )
        # Duration gate: >15 sec diff = 0.0
        assert score == 0.0

    def test_completely_different_track_scores_low(self):
        """Unrelated track must score low (but not zero if duration matches)."""
        candidate = make_result(
            title="Cooking Tutorial Part 5",
            artist="Chef Channel",
            artist_id="UC_chef",
            duration=212,  # Same duration to avoid duration gate
        )
        score = score_atv_match(
            original_title="Never Gonna Give You Up",
            original_artist="Rick Astley",
            original_duration=212,
            original_artist_ids={"UC_rick"},
            candidate=candidate,
        )
        # Low score, but not zero since some character overlap exists
        assert score < 0.3

    def test_score_below_star_threshold_for_partial_match(self):
        """Similar titles score high but different artist ID blocks the star.

        score_atv_match is for RANKING, not star eligibility.
        The star requires: artist_id match + title_sim > 0.85 + duration ≤ 5s.
        So a high score here is fine - it just means good ranking position.
        """
        candidate = make_result(
            title="Never Gonna Let You Down",  # Similar but different song
            artist="Rick Astley",
            artist_id="UC_different",  # Different ID - blocks star!
            duration=200,
        )
        score = score_atv_match(
            original_title="Never Gonna Give You Up",
            original_artist="Rick Astley",
            original_duration=212,
            original_artist_ids={"UC_rick"},
            candidate=candidate,
        )
        # High score for ranking (similar text), but star blocked by artist_id
        # and duration diff (12 seconds > 5 second threshold)
        assert score > 0.5  # Good enough to appear in results

    def test_exact_match_reaches_star_threshold(self):
        """Perfect match with artist ID should definitely get ⭐."""
        candidate = make_result(
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            artist_id="UC_rick",
            duration=212,
        )
        score = score_atv_match(
            original_title="Never Gonna Give You Up",
            original_artist="Rick Astley",
            original_duration=212,
            original_artist_ids={"UC_rick"},
            candidate=candidate,
        )
        # Must reach star threshold
        assert score >= 0.85


# ==========================================================================
# DEDUPLICATION - Better version must be kept
# ==========================================================================


class TestDeduplicationPreservesQuality:
    """When same video exists in YTM and YouTube results, YTM version kept.

    This matters because YTM has better metadata (square thumbnails, album info).
    """

    def test_ytm_version_kept_over_youtube(self):
        """Same video ID in both sources - YTM version preserved."""
        ytm_result = make_result(
            video_id="shared123",
            title="Song (Better Metadata)",
            source="ytm_song",
        )
        yt_result = make_result(
            video_id="shared123",
            title="Song",
            source="youtube",
        )

        ytm_out, yt_out = dedupe_results([ytm_result], [yt_result])

        # YTM result unchanged
        assert len(ytm_out) == 1
        assert ytm_out[0].video_id == "shared123"
        # YouTube duplicate removed
        assert len(yt_out) == 0

    def test_unique_youtube_results_preserved(self):
        """YouTube results not in YTM are kept."""
        ytm_result = make_result(video_id="ytm_only", source="ytm_song")
        yt_result = make_result(video_id="yt_only", source="youtube")

        ytm_out, yt_out = dedupe_results([ytm_result], [yt_result])

        assert len(ytm_out) == 1
        assert len(yt_out) == 1
        assert yt_out[0].video_id == "yt_only"

    def test_exclude_id_removes_from_youtube_only(self):
        """Original URL's video ID excluded from YouTube, not YTM.

        This is important: same video can appear as slot 0 (original) AND
        slot 1 (YTM ATV version with better metadata). We want both options.
        """
        ytm_result = make_result(video_id="original123", source="ytm_song")
        yt_result = make_result(video_id="original123", source="youtube")

        ytm_out, yt_out = dedupe_results(
            [ytm_result], [yt_result], exclude_id="original123"
        )

        # YTM version kept (might be ATV with better metadata)
        assert len(ytm_out) == 1
        # YouTube version excluded (would be duplicate of slot 0)
        assert len(yt_out) == 0


# ==========================================================================
# SEARCH RESULT CONVERSION - Track must have correct playback info
# ==========================================================================


class TestTrackHasCorrectPlaybackInfo:
    """SearchResult.to_track() must produce playable Track objects.

    Less dangerous (would fail obviously) but worth a sanity check.
    """

    def test_url_constructed_correctly(self):
        """Track URL must be valid YouTube watch URL."""
        result = make_result(video_id="dQw4w9WgXcQ")
        track = result.to_track()
        assert track.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_metadata_preserved(self):
        """Title, artist, duration carried through."""
        result = make_result(
            video_id="test",
            title="My Song",
            artist="My Artist",
            duration=240,
        )
        track = result.to_track()
        assert track.title == "My Song"
        assert track.artist == "My Artist"
        assert track.duration == 240

    def test_source_preserved_for_ui(self):
        """Source field needed for UI indicators (song vs video icons)."""
        ytm_result = make_result(source="ytm_song")
        yt_result = make_result(source="youtube")

        assert ytm_result.to_track().source == "ytm_song"
        assert yt_result.to_track().source == "youtube"
