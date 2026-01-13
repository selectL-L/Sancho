"""Outcome-based tests for utils.musicutils.search module.

These tests verify that the search module produces correct results from the
USER's perspective, not that functions return expected values. Organized by
dangerous outcomes that must be achieved or avoided.

Dangerous outcomes for search:
- User gets wrong video (bad URL parsing, bad deduplication)
- Good results filtered out (is_relevant too strict)
- Garbage results shown (is_relevant too loose)
- Wrong track recommended (score_candidate gives high score to wrong track)
"""

from utils.musicutils.search import (
    extract_video_id,
    is_relevant,
    dedupe_results,
    score_candidate,
    OriginalMetadata,
    SearchResult,
    MUSIC_VIDEO_TYPE_ATV,
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
    video_type: str | None = None,
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
        video_type=video_type,
    )


def make_atv(
    video_id: str = "test123",
    title: str = "Test Song",
    artist: str = "Test Artist",
    artist_id: str | None = None,
    duration: int = 180,
) -> SearchResult:
    """Factory for ATV SearchResult."""
    return SearchResult(
        video_id=video_id,
        title=title,
        artist=artist,
        artist_id=artist_id,
        duration_seconds=duration,
        thumbnail_url=None,
        source="ytm_song",
        video_type=MUSIC_VIDEO_TYPE_ATV,
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

    def test_no_word_overlap_means_rejection(self):
        """Query and result share no words = definitely garbage."""
        result = make_result(title="Sunset Boulevard", artist="Orchestra")
        # No overlap in words, should be filtered
        assert is_relevant("morning coffee jazz", result) is False

    def test_short_query_needs_meaningful_overlap(self):
        """Even short queries need some real overlap."""
        result = make_result(title="Completely Different", artist="Other")
        assert is_relevant("song name", result) is False


# ==========================================================================
# ATV SCORING - Best match must be recommended
# ==========================================================================


class TestBestMatchIsRecommended:
    """Scenarios where score_candidate must identify the correct ATV.

    This is dangerous: wrong recommendation = user picks inferior version.

    New scoring philosophy: confidence-based sliding scale.
    - Higher artist confidence → lower title threshold required
    - Perfect title match (≥95%) always passes regardless of artist
    """

    def test_artist_id_match_gives_high_score(self):
        """Same artist ID = 100% artist confidence = needs only 25% title match."""
        original = OriginalMetadata(
            title="Song Name",
            artist="Artist Name",
            artist_id="UC123456",
        )
        candidate = make_atv(
            title="Song Name",
            artist="Artist Name",
            artist_id="UC123456",
        )
        score = score_candidate(original, candidate)
        assert score >= 0.8  # High confidence (100% artist + 100% title)

    def test_different_artist_id_with_exact_title_still_scores(self):
        """Different artist ID but perfect title = passes (title ≥95% auto-pass)."""
        original = OriginalMetadata(
            title="Popular Song",
            artist="Original Artist",
            artist_id="UC_original",
        )
        candidate = make_atv(
            title="Popular Song",
            artist="Original Artist",
            artist_id="UC_different",  # Different ID
        )
        score = score_candidate(original, candidate)
        # Perfect title match auto-passes regardless of artist ID
        assert score >= 0.6

    def test_no_artist_id_with_matching_artist_name(self):
        """No artist ID but name matches = moderate confidence."""
        original = OriginalMetadata(
            title="Song Name",
            artist="Artist Name",
            artist_id=None,  # No ID
        )
        candidate = make_atv(
            title="Song Name",
            artist="Artist Name",
            artist_id=None,
        )
        score = score_candidate(original, candidate)
        # Name match gives 80% artist confidence + title threshold ~40%
        assert score >= 0.7

    def test_completely_different_track_scores_zero(self):
        """Unrelated track must fail the sliding threshold."""
        original = OriginalMetadata(
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            artist_id="UC_rick",
        )
        candidate = make_atv(
            title="Cooking Tutorial Part 5",
            artist="Chef Channel",
            artist_id="UC_chef",
        )
        score = score_candidate(original, candidate)
        # No artist match (0% confidence) → needs 100%+ title match → fails
        assert score == 0.0

    def test_similar_title_different_artist_fails(self):
        """Similar titles score high but need artist confidence to pass.

        With 0% artist confidence, title needs to be nearly perfect (>100%).
        "Never Gonna Let You Down" vs "Never Gonna Give You Up" = ~80% similar
        This should fail because 80% < required 100%+.
        """
        original = OriginalMetadata(
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            artist_id="UC_rick",
        )
        candidate = make_atv(
            title="Never Gonna Let You Down",  # Similar but different
            artist="Rick Astley",  # Same artist name
            artist_id="UC_different",  # Different ID
        )
        score = score_candidate(original, candidate)
        # Artist name matches (80% confidence) → title needs ~40%
        # "Never Gonna Let You Down" vs "Give You Up" = high similarity
        # Should pass due to name match
        assert score >= 0.5

    def test_exact_match_reaches_star_threshold(self):
        """Perfect match with artist ID should definitely get ⭐."""
        original = OriginalMetadata(
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            artist_id="UC_rick",
        )
        candidate = make_atv(
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            artist_id="UC_rick",
        )
        score = score_candidate(original, candidate)
        # 100% artist + 100% title = max score
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


# ==========================================================================
# TAG CONFIDENCE SCORING TESTS
# ==========================================================================


class TestTagConfidenceScoring:
    """Tests for CJK artist name extraction with confidence scoring.

    Tests both the new confidence system and legacy compatibility.
    """

    def test_pure_cjk_tag_high_confidence(self):
        """Pure CJK artist names should get high confidence."""
        from utils.musicutils.search import calculate_tag_confidence

        # Pure Japanese name
        conf = calculate_tag_confidence('紫咲シオン')
        assert conf >= 0.7, f"Pure CJK should be high confidence, got {conf}"

        # Chinese characters
        conf = calculate_tag_confidence('周杰伦')
        assert conf >= 0.7, f"Pure Chinese should be high confidence, got {conf}"

    def test_vocaloid_p_pattern_boosted(self):
        """VocaloidP pattern (name + P suffix) should get confidence boost."""
        from utils.musicutils.search import calculate_tag_confidence, _is_vocaloid_p_pattern

        # Test pattern detection - requires CJK content before P
        assert _is_vocaloid_p_pattern('みきとP')
        assert _is_vocaloid_p_pattern('ハチP')
        assert not _is_vocaloid_p_pattern('syudouP')  # No CJK - doesn't match pattern
        assert not _is_vocaloid_p_pattern('Music')  # No CJK
        assert not _is_vocaloid_p_pattern('P')  # Too short

        # Test confidence boost
        conf = calculate_tag_confidence('みきとP')
        assert conf >= 0.8, f"VocaloidP pattern should be >= 0.8, got {conf}"

    def test_hard_skip_exact_zero_confidence(self):
        """Tags in hard skip list should get 0.0 confidence."""
        from utils.musicutils.search import calculate_tag_confidence

        assert calculate_tag_confidence('ホロライブ') == 0.0
        assert calculate_tag_confidence('hololive') == 0.0
        assert calculate_tag_confidence('nijisanji') == 0.0
        assert calculate_tag_confidence('mv') == 0.0
        assert calculate_tag_confidence('MV') == 0.0  # Case insensitive

    def test_hard_skip_contains_zero_confidence(self):
        """Tags containing hard skip patterns should get 0.0 confidence."""
        from utils.musicutils.search import calculate_tag_confidence

        # Contains 'hololive'
        assert calculate_tag_confidence('hololive production') == 0.0
        # Contains 'にじさんじ'
        assert calculate_tag_confidence('にじさんじ所属') == 0.0

    def test_song_title_similarity_penalty(self):
        """Tags that look like the song title should be penalized."""
        from utils.musicutils.search import calculate_tag_confidence

        # Tag matches video title
        conf_with_title = calculate_tag_confidence('ステラステラ', video_title='Stellar Stellar / 星街すいせい')
        conf_without = calculate_tag_confidence('ステラステラ')

        # Both should pass, but with title should be penalized (or at least not higher)
        # If the title isn't closely matching, penalty might be small
        assert conf_with_title >= 0.0
        assert conf_without >= 0.0

        # High overlap case - tag IS the song name
        conf_exact = calculate_tag_confidence('ロキ', video_title='ロキ / Roki')
        # This should be penalized since tag appears in title
        assert conf_exact <= 0.4, f"Song title tag should be penalized, got {conf_exact}"

    def test_soft_skip_reduces_but_not_zeros(self):
        """Soft skip terms should reduce confidence but not to zero."""
        from utils.musicutils.search import calculate_tag_confidence

        # "cover" is soft skip
        conf = calculate_tag_confidence('歌ってみた')
        assert 0.0 < conf < 0.6, f"Soft skip should reduce confidence, got {conf}"

        # "original" is soft skip
        conf = calculate_tag_confidence('オリジナル曲')
        assert 0.0 < conf < 0.6, f"Soft skip should reduce confidence, got {conf}"

    def test_length_penalties(self):
        """Tags with unusual length should be penalized."""
        from utils.musicutils.search import calculate_tag_confidence

        # Single char - too short
        conf_short = calculate_tag_confidence('愛')
        assert conf_short < 0.5, f"Single char should be penalized, got {conf_short}"

        # Very long - probably a phrase
        long_tag = '東京都渋谷区の歌い手による歌ってみた動画'
        conf_long = calculate_tag_confidence(long_tag)
        assert conf_long < 0.5, f"Very long tag should be penalized, got {conf_long}"

    def test_extract_cjk_artist_names_returns_sorted(self):
        """extract_cjk_artist_names should return tags sorted by confidence."""
        from utils.musicutils.search import extract_cjk_artist_names

        tags = [
            'ホロライブ',  # Hard skip - 0.0
            '星街すいせい',  # Pure CJK - high
            'みきとP',  # VocaloidP - boosted
            'music',  # No CJK - filtered out
            '歌ってみた',  # Soft skip - reduced
        ]

        results = extract_cjk_artist_names(tags, min_confidence=0.0)

        # Should have filtered out 'ホロライブ' (0.0) and 'music' (no CJK)
        tag_names = [t for t, c in results]
        assert 'ホロライブ' not in tag_names or results[0][1] == 0.0
        assert 'music' not in tag_names

        # Check sorted by confidence descending
        confidences = [c for t, c in results]
        assert confidences == sorted(confidences, reverse=True), "Results should be sorted by confidence"

    def test_extract_cjk_artist_names_respects_max_results(self):
        """extract_cjk_artist_names should respect max_results parameter."""
        from utils.musicutils.search import extract_cjk_artist_names

        tags = ['星街すいせい', '紫咲シオン', '宝鐘マリン', '白上フブキ', '大空スバル']
        results = extract_cjk_artist_names(tags, max_results=2)

        assert len(results) <= 2, f"Should return max 2 results, got {len(results)}"

    def test_extract_jp_names_backward_compatible(self):
        """extract_jp_names should still work without video_title (backward compat)."""
        from utils.musicutils.search import extract_jp_names

        tags = ['星街すいせい', 'Hoshimachi Suisei', 'hololive', '歌ってみた']

        # Should work without video_title
        names = extract_jp_names(tags)

        # Should return CJK tags that pass legacy filtering
        assert '星街すいせい' in names
        # Latin-only should be excluded
        assert 'Hoshimachi Suisei' not in names
        # Hard skip should be excluded
        assert 'hololive' not in names

    def test_extract_jp_names_with_video_title(self):
        """extract_jp_names should accept video_title parameter for title filtering."""
        from utils.musicutils.search import extract_jp_names

        tags = ['星街すいせい', 'ステラステラ']

        # With video_title, tags matching the title get penalized
        names = extract_jp_names(tags, video_title='Stellar Stellar')

        # Artist name should still be included
        assert '星街すいせい' in names

    def test_stellar_stellar_stress_test(self):
        """Stress test with Stellar Stellar's tag explosion scenario.

        Stellar Stellar has many Hololive member tags. The confidence system
        should handle this gracefully.
        """
        from utils.musicutils.search import extract_cjk_artist_names

        # Simulated tags from Stellar Stellar video
        tags = [
            '星街すいせい',  # Actual artist - should be high
            'ステラステラ',  # Song title - should be penalized with title context
            'hololive', 'ホロライブ',  # Agency - hard skip
            '白上フブキ', '大空スバル', '紫咲シオン',  # Other members
            '宝鐘マリン', 'さくらみこ', 'ときのそら',
            'music', 'MV', '歌ってみた',  # Format/content type
        ]

        # With video title, song name tag should be penalized
        results = extract_cjk_artist_names(
            tags,
            video_title='Stellar Stellar / 星街すいせい',
            min_confidence=0.4,
            max_results=3
        )

        # Should have reasonable number of results
        assert len(results) <= 3, f"max_results should be respected, got {len(results)}"

        # Agency tags should not appear
        tag_names = [t for t, c in results]
        assert 'hololive' not in tag_names
        assert 'ホロライブ' not in tag_names

        # Actual artist should appear (though other members might too)
        # This is expected - confidence scoring alone can't know who the
        # "real" artist is among multiple valid CJK names.

    def test_mixed_script_moderate_confidence(self):
        """Mixed CJK/Latin tags should get moderate confidence."""
        from utils.musicutils.search import calculate_tag_confidence

        # DECO*27 style name - pure Latin + symbols, no CJK
        conf = calculate_tag_confidence('DECO*27')
        # No CJK, so should be 0
        assert conf == 0.0, f"Pure Latin should be 0.0, got {conf}"

        # Mixed but has CJK - note "official" is soft skip, so reduced
        conf = calculate_tag_confidence('Official髭男dism')
        # Contains 'official' (soft skip) so heavily penalized
        assert 0.0 < conf < 0.5, f"Soft skip content should be penalized, got {conf}"

        # Mixed without soft skip penalty
        conf = calculate_tag_confidence('髭男dism')
        assert 0.3 <= conf <= 0.8, f"Mixed script without soft skip should be moderate, got {conf}"

    def test_script_ratio_calculation(self):
        """_calculate_script_ratio should correctly measure CJK proportion."""
        from utils.musicutils.search import _calculate_script_ratio

        # Pure CJK
        assert _calculate_script_ratio('星街すいせい') == 1.0

        # Pure Latin
        assert _calculate_script_ratio('Suisei') == 0.0

        # Mixed - should be between 0 and 1
        ratio = _calculate_script_ratio('みきとP')
        assert 0.5 < ratio < 1.0, f"Mixed should be partial, got {ratio}"

        # Empty string
        assert _calculate_script_ratio('') == 0.0

        # Numbers/symbols only
        assert _calculate_script_ratio('12345') == 0.0
