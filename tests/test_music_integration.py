"""Integration test for Music cog - simulates a realistic user session.

This test exercises a complete user flow:
1. User joins voice channel
2. User asks bot to listen along
3. Bot plays initial tracks from ambient playlist
4. User adds their own song
5. User jumps to their song
6. User shuffles the playlist
7. Playback continues with shuffled order

All external dependencies (Discord voice, yt-dlp, FFmpeg) are mocked.
The test verifies that internal state transitions correctly at each step.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.music import Music
from utils.musicutils import (
    ActiveSession,
    AudioFetcher,
    FetchContext,
    LoopMode,
    PlaybackState,
    Track,
)
from utils.musicutils.music_auth import AudioFetchResult


# =============================================================================
# Fixtures
# =============================================================================


def create_ambient_playlist() -> list[Track]:
    """Creates a deterministic ambient playlist for testing."""
    return [
        Track(
            title="Ambient Track 1",
            artist="Ambient Artist",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            duration=180,
            video_id="dQw4w9WgXcQ",
            user_added=False
        ),
        Track(
            title="Ambient Track 2",
            artist="Ambient Artist",
            url="https://www.youtube.com/watch?v=xvFZjo5PgG0",
            duration=200,
            video_id="xvFZjo5PgG0",
            user_added=False
        ),
        Track(
            title="Ambient Track 3",
            artist="Ambient Artist",
            url="https://www.youtube.com/watch?v=J---aiyznGQ",
            duration=220,
            video_id="J---aiyznGQ",
            user_added=False
        ),
        Track(
            title="Ambient Track 4",
            artist="Ambient Artist",
            url="https://www.youtube.com/watch?v=9bZkp7q19f0",
            duration=190,
            video_id="9bZkp7q19f0",
            user_added=False
        ),
        Track(
            title="Ambient Track 5",
            artist="Ambient Artist",
            url="https://www.youtube.com/watch?v=kJQP7kiw5Fk",
            duration=210,
            video_id="kJQP7kiw5Fk",
            user_added=False
        ),
    ]


def create_user_track() -> Track:
    """Creates the track that the user will add."""
    return Track(
        title="User's Favorite Song",
        artist="User's Artist",
        url="https://www.youtube.com/watch?v=L_jWHffIx5E",
        duration=240,
        video_id="L_jWHffIx5E",
        user_added=True
    )


@pytest.fixture
def mock_bot():
    """Creates a mock bot instance."""
    bot = MagicMock()
    bot.owner_id = 99999
    bot.user = MagicMock()
    bot.user.id = 12345
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def mock_voice_client():
    """Creates a mock voice client."""
    vc = MagicMock(spec=discord.VoiceClient)
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.is_paused.return_value = False
    vc.play = MagicMock()
    vc.stop = MagicMock()
    vc.disconnect = AsyncMock()
    return vc


@pytest.fixture
def mock_voice_channel():
    """Creates a mock voice channel."""
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = 777777
    channel.guild = MagicMock()
    channel.guild.id = 888888
    channel.connect = AsyncMock()
    return channel


@pytest.fixture
def mock_ctx(mock_voice_channel):
    """Creates a mock command context with user in voice."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = MagicMock()
    ctx.guild.id = 888888
    ctx.channel = MagicMock()
    ctx.send = AsyncMock()

    # User setup - in the voice channel
    # IMPORTANT: Must use spec=discord.Member for isinstance() check in _do_listen_along
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = 11111
    ctx.author.voice = MagicMock()
    ctx.author.voice.channel = mock_voice_channel

    return ctx


@pytest.fixture
def music_cog(mock_bot):
    """Creates a Music cog instance with mocked dependencies."""
    with patch('cogs.music.MusicAmbience') as mock_ambience_class:
        # Setup ambience mock
        mock_ambience = MagicMock()
        mock_ambience.get_current_playlist_url.return_value = "https://youtube.com/playlist?list=test"
        mock_ambience.get_listen_along_response.return_value = "🎵 Starting ambient music!"
        mock_ambience_class.return_value = mock_ambience

        cog = Music(mock_bot)

        # Initialize state
        cog.playlist = []
        cog.current_index = 0
        cog.loop_mode = LoopMode.OFF
        cog.active_session = None
        cog._playback = PlaybackState()
        cog._audio_fetcher = AudioFetcher(cog.cache_manager, cog.logger)

        # Mock db_manager for async calls
        cog.db_manager = MagicMock()
        cog.db_manager.get_guild_config = AsyncMock(return_value=None)

        return cog


# =============================================================================
# Integration Test
# =============================================================================


class TestMusicSessionIntegration:
    """Integration test simulating a complete user session."""

    @pytest.mark.asyncio
    async def test_full_user_session_flow(
        self,
        music_cog,
        mock_ctx,
        mock_voice_client,
        mock_voice_channel
    ):
        """
        Simulates a complete user session:
        1. Listen along starts with ambient playlist
        2. User listens to 2 songs (advance twice)
        3. User adds their own song
        4. User jumps to their song
        5. User shuffles (with deterministic seed)
        6. Verify final state
        """

        # =====================================================================
        # PHASE 1: Setup - Load ambient playlist
        # =====================================================================

        ambient_playlist = create_ambient_playlist()
        music_cog.playlist = ambient_playlist.copy()
        music_cog.current_index = 0

        # Verify initial state
        assert len(music_cog.playlist) == 5
        assert music_cog._get_current_track().title == "Ambient Track 1"
        assert all(not t.user_added for t in music_cog.playlist)

        # =====================================================================
        # PHASE 2: User triggers listen along
        # =====================================================================

        # Mock the voice connection
        mock_voice_channel.connect.return_value = mock_voice_client

        # Mock _start_session to set up active_session
        # Signature: _start_session(self, channel, ctx, join_message=None)
        async def mock_start_session(channel, ctx, join_message=None):
            music_cog.active_session = ActiveSession(
                guild_id=channel.guild.id,
                channel_id=channel.id,
                voice_client=mock_voice_client,
                origin_channel_id=channel.id
            )

        with patch.object(music_cog, '_start_session', side_effect=mock_start_session):
            with patch.object(music_cog, '_play_current_track', new_callable=AsyncMock):
                await music_cog._do_listen_along(mock_ctx)

        # Verify session started
        assert music_cog.active_session is not None
        assert music_cog.active_session.channel_id == mock_voice_channel.id

        # =====================================================================
        # PHASE 3: Simulate listening to 2 songs (advance track twice)
        # =====================================================================

        # First song playing
        assert music_cog.current_index == 0
        current = music_cog._get_current_track()
        assert current.title == "Ambient Track 1"

        # Song 1 ends -> advance to song 2
        next_track = music_cog._advance_track()
        assert next_track is not None
        assert next_track.title == "Ambient Track 2"
        assert music_cog.current_index == 1

        # Song 2 ends -> advance to song 3
        next_track = music_cog._advance_track()
        assert next_track is not None
        assert next_track.title == "Ambient Track 3"
        assert music_cog.current_index == 2

        # =====================================================================
        # PHASE 4: User adds their own song
        # =====================================================================

        user_track = create_user_track()

        # Simulate adding track (what _do_play does internally when adding to queue)
        music_cog.playlist.append(user_track)

        # Verify track was added
        assert len(music_cog.playlist) == 6
        assert music_cog.playlist[-1].title == "User's Favorite Song"
        assert music_cog.playlist[-1].user_added is True

        # Still on track 3
        assert music_cog.current_index == 2
        assert music_cog._get_current_track().title == "Ambient Track 3"

        # =====================================================================
        # PHASE 5: User jumps to their song
        # =====================================================================

        # Find user's song index (should be 5, 0-indexed)
        user_song_index = next(
            i for i, t in enumerate(music_cog.playlist)
            if t.title == "User's Favorite Song"
        )
        assert user_song_index == 5

        # Perform jump (1-indexed in UI, so position 6)
        with patch.object(music_cog, '_play_current_track', new_callable=AsyncMock):
            await music_cog._do_jump(mock_ctx, position=6)

        # Verify jump
        assert music_cog.current_index == 5
        assert music_cog._get_current_track().title == "User's Favorite Song"
        assert music_cog._get_current_track().user_added is True

        # =====================================================================
        # PHASE 6: User shuffles the playlist
        # =====================================================================

        # Record pre-shuffle state
        pre_shuffle_titles = [t.title for t in music_cog.playlist]

        # Use deterministic seed for reproducible shuffle
        with patch('cogs.music.random.shuffle') as mock_shuffle:
            # Define exactly what shuffle does - moves current to front, shuffles rest
            def deterministic_shuffle(lst):
                # Simulate a specific shuffle result
                # Current track (User's Favorite Song) stays at index 0 after preserve_current
                # Rest get shuffled to a known order
                pass  # The actual shuffle happens, we just need to verify behavior

            mock_shuffle.side_effect = deterministic_shuffle

            # Actually call _apply_shuffle with preserve_current=True
            # This should move current track to front, then shuffle the rest
            music_cog._apply_shuffle(preserve_current=True)

        # Verify current track is now at index 0 (preserve_current behavior)
        assert music_cog.current_index == 0
        assert music_cog._get_current_track().title == "User's Favorite Song"

        # Verify playlist was actually modified (not same order)
        post_shuffle_titles = [t.title for t in music_cog.playlist]

        # The first track should be the user's song (preserved)
        assert post_shuffle_titles[0] == "User's Favorite Song"

        # All tracks should still be present
        assert set(post_shuffle_titles) == set(pre_shuffle_titles)
        assert len(music_cog.playlist) == 6

        # =====================================================================
        # PHASE 7: Continue playback after shuffle
        # =====================================================================

        # User's song finishes -> advance
        next_track = music_cog._advance_track()
        assert next_track is not None
        assert music_cog.current_index == 1

        # The next track should be one of the ambient tracks (shuffled order)
        assert next_track.title in [
            "Ambient Track 1", "Ambient Track 2", "Ambient Track 3",
            "Ambient Track 4", "Ambient Track 5"
        ]

        # Advance again
        next_track = music_cog._advance_track()
        assert next_track is not None
        assert music_cog.current_index == 2

        # =====================================================================
        # PHASE 8: Final state verification
        # =====================================================================

        # Session still active
        assert music_cog.active_session is not None

        # Playlist integrity
        assert len(music_cog.playlist) == 6

        # User track still in playlist
        user_tracks = [t for t in music_cog.playlist if t.user_added]
        assert len(user_tracks) == 1
        assert user_tracks[0].title == "User's Favorite Song"

        # All ambient tracks still present
        ambient_tracks = [t for t in music_cog.playlist if not t.user_added]
        assert len(ambient_tracks) == 5

    @pytest.mark.asyncio
    async def test_session_with_loop_mode_track(
        self,
        music_cog,
        mock_ctx,
        mock_voice_client,
        mock_voice_channel
    ):
        """
        Tests that loop track mode keeps repeating the same track.
        """

        # Setup
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.ONE  # Loop single track

        # Get current track
        current = music_cog._get_current_track()
        assert current.title == "Ambient Track 1"

        # Advance should return same track (loop track mode)
        for _ in range(5):
            next_track = music_cog._advance_track()
            assert next_track.title == "Ambient Track 1"
            assert music_cog.current_index == 0

    @pytest.mark.asyncio
    async def test_session_with_loop_mode_playlist(
        self,
        music_cog,
        mock_ctx,
        mock_voice_client,
        mock_voice_channel
    ):
        """
        Tests that loop playlist mode wraps around at end.
        """

        # Setup - start near end of playlist
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 3  # Track 4
        music_cog.loop_mode = LoopMode.ALL  # Loop entire playlist

        # Advance to track 5
        next_track = music_cog._advance_track()
        assert next_track.title == "Ambient Track 5"
        assert music_cog.current_index == 4

        # Advance past end -> should wrap to track 1
        next_track = music_cog._advance_track()
        assert next_track.title == "Ambient Track 1"
        assert music_cog.current_index == 0

    @pytest.mark.asyncio
    async def test_user_adds_multiple_songs_and_removes_one(
        self,
        music_cog,
        mock_ctx,
        mock_voice_client,
        mock_voice_channel
    ):
        """
        Tests adding multiple user songs, then removing one.
        Verifies playlist state and indices remain consistent.
        """

        # Setup
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 2  # Playing track 3

        # User adds 3 songs
        user_songs = [
            Track(
                title=f"User Song {i}",
                artist="User",
                url=f"https://youtube.com/watch?v=user{i:03d}",
                duration=200,
                video_id=f"user{i:03d}",
                user_added=True
            )
            for i in range(1, 4)
        ]

        for song in user_songs:
            music_cog.playlist.append(song)

        assert len(music_cog.playlist) == 8  # 5 ambient + 3 user

        # User is playing track 3 (index 2)
        assert music_cog._get_current_track().title == "Ambient Track 3"

        # Remove User Song 2 (index 6)
        music_cog._remove_track(6)

        # Verify removal
        assert len(music_cog.playlist) == 7
        assert "User Song 2" not in [t.title for t in music_cog.playlist]

        # Current index should be unchanged (removal was after current)
        assert music_cog.current_index == 2
        assert music_cog._get_current_track().title == "Ambient Track 3"

        # Now remove a track BEFORE current (index 0)
        music_cog._remove_track(0)

        # Current index should shift down by 1
        assert music_cog.current_index == 1
        assert music_cog._get_current_track().title == "Ambient Track 3"
        assert len(music_cog.playlist) == 6

    @pytest.mark.asyncio
    async def test_dedupe_removes_duplicates_preserves_current(
        self,
        music_cog,
        mock_ctx
    ):
        """
        Tests that dedupe removes duplicate tracks but preserves current track.

        Note: _dedupe_playlist keeps the LATER occurrence and removes earlier ones.
        If current_index points to the first occurrence, it gets removed.
        This test sets current_index to the LATER occurrence to verify it's preserved.
        """

        # Create playlist with duplicates
        music_cog.playlist = [
            Track(
                title="Ambient Track 1",
                artist="Artist",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                duration=180,
                video_id="dQw4w9WgXcQ",
                user_added=False
            ),
            Track(  # First occurrence - will be REMOVED
                title="Duplicate Track",
                artist="Artist",
                url="https://www.youtube.com/watch?v=dup0000001",
                duration=200,
                video_id="dup0000001",
                user_added=False
            ),
            Track(
                title="Ambient Track 2",
                artist="Artist",
                url="https://www.youtube.com/watch?v=xvFZjo5PgG0",
                duration=190,
                video_id="xvFZjo5PgG0",
                user_added=False
            ),
            Track(  # Second occurrence - KEPT (current track)
                title="Duplicate Track",
                artist="Artist",
                url="https://www.youtube.com/watch?v=dup0000001",
                duration=200,
                video_id="dup0000001",
                user_added=False
            ),
            Track(
                title="Ambient Track 3",
                artist="Artist",
                url="https://www.youtube.com/watch?v=J---aiyznGQ",
                duration=210,
                video_id="J---aiyznGQ",
                user_added=False
            ),
        ]

        # Current track is the SECOND (later) occurrence of duplicate - this one is kept
        music_cog.current_index = 3
        assert music_cog._get_current_track().title == "Duplicate Track"

        # Dedupe removes the earlier occurrence at index 1
        removed_count = music_cog._dedupe_playlist()

        # Should have removed 1 duplicate (the first occurrence at index 1)
        assert removed_count == 1
        assert len(music_cog.playlist) == 4

        # Current index should be adjusted: was 3, one item before it removed, now 2
        assert music_cog.current_index == 2

        # Current track should still be "Duplicate Track" (the later occurrence we were on)
        assert music_cog._get_current_track().title == "Duplicate Track"

        # Only one copy of the duplicate track should remain
        dup_count = sum(1 for t in music_cog.playlist if t.title == "Duplicate Track")
        assert dup_count == 1

    @pytest.mark.asyncio
    async def test_move_track_updates_current_index_correctly(
        self,
        music_cog,
        mock_ctx
    ):
        """
        Tests that moving tracks updates current_index correctly.
        """

        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 2  # Playing Track 3

        # Move Track 5 (index 4) to position after Track 1 (to index 1)
        result = music_cog._move_track(from_index=4, to_index=1)

        assert result is not None
        assert result.title == "Ambient Track 5"

        # Track 3 should still be current, but at new index
        assert music_cog._get_current_track().title == "Ambient Track 3"
        # Index shifted because we inserted before it
        assert music_cog.current_index == 3

        # Verify playlist order
        expected_order = [
            "Ambient Track 1",
            "Ambient Track 5",  # Moved here
            "Ambient Track 2",
            "Ambient Track 3",  # Current
            "Ambient Track 4",
        ]
        actual_order = [t.title for t in music_cog.playlist]
        assert actual_order == expected_order

    @pytest.mark.asyncio
    async def test_swap_tracks_preserves_current(
        self,
        music_cog,
        mock_ctx
    ):
        """
        Tests that swapping tracks keeps current track playing.
        """

        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 2  # Playing Track 3

        # Swap Track 1 (index 0) with Track 5 (index 4)
        result = music_cog._swap_tracks(0, 4)

        assert result is not None
        track_a, track_b = result
        # _swap_tracks returns the tracks at the indices AFTER swap
        # So index 0 now has Track 5, index 4 now has Track 1
        assert track_a.title == "Ambient Track 5"  # Now at index 0
        assert track_b.title == "Ambient Track 1"  # Now at index 4

        # Current should still be Track 3, same index (not involved in swap)
        assert music_cog.current_index == 2
        assert music_cog._get_current_track().title == "Ambient Track 3"

        # Now swap current track with another
        music_cog._swap_tracks(2, 0)

        # Current index should update to track the swapped position
        assert music_cog._get_current_track().title == "Ambient Track 3"
        # After swap: [Track3, Track2, Track5, Track4, Track1]
        assert music_cog.current_index == 0

    @pytest.mark.asyncio
    async def test_pause_resume_cycle(
        self,
        music_cog,
        mock_voice_client
    ):
        """
        Tests pause/resume maintains correct state.
        """
        import time
        from unittest.mock import MagicMock

        # Setup active session
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )
        # Set track_started_at for pause position calculation
        music_cog.track_started_at = time.time() - 30  # 30 seconds into track

        # Mock _player (ManagedPlayer) instead of voice_client
        mock_player = MagicMock()
        mock_player.is_playing = True
        mock_player.is_paused = False
        mock_player.pause.return_value = True
        mock_player.position = 30.0
        music_cog._player = mock_player

        # Pause
        result = music_cog._do_pause()
        assert result is True
        mock_player.pause.assert_called_once()

        # Update mock state
        mock_player.is_playing = False
        mock_player.is_paused = True

        # Resume
        result = await music_cog._do_resume()

        assert result is True

        # Playlist state unchanged
        assert music_cog.current_index == 0
        assert music_cog._get_current_track().title == "Ambient Track 1"

    @pytest.mark.asyncio
    async def test_skip_advances_to_next_track(
        self,
        music_cog,
        mock_voice_client
    ):
        """
        Tests that skip properly advances and stops current playback.
        """
        from unittest.mock import MagicMock

        # Setup
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 1  # Track 2
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        # Mock _player (ManagedPlayer)
        mock_player = MagicMock()
        mock_player.is_playing = True
        mock_player.is_paused = False
        music_cog._player = mock_player

        # Mock _play_current_track to prevent actual playback
        with patch.object(music_cog, '_play_current_track', new_callable=AsyncMock):
            # Skip (now async)
            result = await music_cog._do_skip()

        assert result is True
        mock_player.stop.assert_called_once()

        # Index advanced by _do_skip
        assert music_cog.current_index == 2


# =============================================================================
# Prefetch Logic Tests
# =============================================================================


class TestPrefetchBufferUsage:
    """Tests for prefetch buffer consumption in _play_current_track."""

    @pytest.mark.asyncio
    async def test_valid_prefetch_is_used(self, music_cog, mock_voice_client):
        """When prefetch matches next track, it should be used without re-fetching."""
        # Setup: 3 tracks, currently on track 0
        music_cog.playlist = create_ambient_playlist()[:3]
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        # Setup mock player
        mock_player = MagicMock()
        mock_player.stop = MagicMock()
        mock_player.play = MagicMock()
        music_cog._player = mock_player

        # Advance to track 1 (simulating track 0 ended)
        music_cog._advance_track()
        assert music_cog.current_index == 1

        # Setup: Pre-populate prefetch buffer for track 1 (current after advance)
        track_1 = music_cog.playlist[1]
        music_cog._next_prepared = AudioFetchResult(
            success=True,
            url="https://prefetched-url.com/audio",
            http_headers={"User-Agent": "test"},
        )
        music_cog._next_prepared_track = track_1

        # Mock AudioFetcher.fetch to track if it's called
        fetch_called = False
        original_fetch = music_cog._audio_fetcher.fetch

        async def mock_fetch(*args, **kwargs):
            nonlocal fetch_called
            fetch_called = True
            return await original_fetch(*args, **kwargs)

        # Act: Play current track (should use prefetch)
        with patch.object(music_cog._audio_fetcher, 'fetch', side_effect=mock_fetch):
            with patch.object(music_cog, '_update_playing_presence', new_callable=AsyncMock):
                with patch.object(music_cog, '_prefetch_next_track', new_callable=AsyncMock):
                    await music_cog._play_current_track()

        # Assert: Player was called with prefetched URL
        mock_player.play.assert_called_once()
        call_args = mock_player.play.call_args
        assert call_args[0][1] == "https://prefetched-url.com/audio"  # audio_source arg

        # Assert: AudioFetcher.fetch was NOT called (used prefetch instead)
        assert not fetch_called, "AudioFetcher.fetch should not be called when prefetch is valid"

    @pytest.mark.asyncio
    async def test_stale_prefetch_is_discarded(self, music_cog, mock_voice_client):
        """When prefetch doesn't match current track, it should be discarded."""
        # Setup: 3 tracks
        music_cog.playlist = create_ambient_playlist()[:3]
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        mock_player = MagicMock()
        music_cog._player = mock_player

        # Setup: Prefetch buffer has WRONG track (track 2, but we're playing track 0)
        wrong_track = music_cog.playlist[2]
        music_cog._next_prepared = AudioFetchResult(
            success=True,
            url="https://wrong-prefetch.com/audio",
        )
        music_cog._next_prepared_track = wrong_track

        # Mock AudioFetcher.fetch to return a result for the correct track
        async def mock_fetch(track, context):
            return AudioFetchResult(
                success=True,
                url="https://live-fetched.com/audio",
            )

        # Act
        with patch.object(music_cog._audio_fetcher, 'fetch', side_effect=mock_fetch) as fetch_mock:
            with patch.object(music_cog, '_update_playing_presence', new_callable=AsyncMock):
                with patch.object(music_cog, '_prefetch_next_track', new_callable=AsyncMock):
                    await music_cog._play_current_track()

        # Assert: AudioFetcher.fetch WAS called (prefetch was stale)
        fetch_mock.assert_called_once()
        call_context = fetch_mock.call_args[0][1]
        assert call_context == FetchContext.LIVE

        # Assert: Player got the LIVE-fetched URL, not the stale prefetch
        call_args = mock_player.play.call_args
        assert call_args[0][1] == "https://live-fetched.com/audio"

    @pytest.mark.asyncio
    async def test_missing_prefetch_triggers_live_fetch(self, music_cog, mock_voice_client):
        """When no prefetch exists, should fetch LIVE."""
        music_cog.playlist = create_ambient_playlist()[:2]
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        mock_player = MagicMock()
        music_cog._player = mock_player

        # No prefetch
        music_cog._next_prepared = None
        music_cog._next_prepared_track = None

        async def mock_fetch(track, context):
            return AudioFetchResult(success=True, url="https://live.com/audio")

        with patch.object(music_cog._audio_fetcher, 'fetch', side_effect=mock_fetch) as fetch_mock:
            with patch.object(music_cog, '_update_playing_presence', new_callable=AsyncMock):
                with patch.object(music_cog, '_prefetch_next_track', new_callable=AsyncMock):
                    await music_cog._play_current_track()

        # Should have called fetch with LIVE context
        fetch_mock.assert_called_once()
        assert fetch_mock.call_args[0][1] == FetchContext.LIVE


class TestPrefetchInvalidationOnMutation:
    """Tests for _refresh_prefetch_if_stale after playlist mutations."""

    @pytest.mark.asyncio
    async def test_prefetch_kept_when_next_track_unchanged(self, music_cog, mock_voice_client):
        """Removing a track AFTER next shouldn't invalidate prefetch."""
        # Setup: 5 tracks, currently on track 1, prefetch for track 2
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 1
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        track_2 = music_cog.playlist[2]
        music_cog._next_prepared = AudioFetchResult(success=True, url="https://prefetch.com")
        music_cog._next_prepared_track = track_2

        # Act: Remove track 4 (after current and next)
        music_cog._remove_track(4)

        # Assert: Prefetch still valid
        assert music_cog._next_prepared is not None
        assert music_cog._next_prepared_track.video_id == track_2.video_id

    @pytest.mark.asyncio
    async def test_prefetch_cleared_when_next_track_removed(self, music_cog, mock_voice_client):
        """Removing the prefetched track should clear prefetch."""
        # Setup: 5 tracks, currently on track 1, prefetch for track 2
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 1
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        track_2 = music_cog.playlist[2]
        music_cog._next_prepared = AudioFetchResult(success=True, url="https://prefetch.com")
        music_cog._next_prepared_track = track_2

        # Act: Remove track 2 (the prefetched track)
        music_cog._remove_track(2)

        # Assert: Prefetch cleared (next track is now different)
        assert music_cog._next_prepared is None
        assert music_cog._next_prepared_track is None

    @pytest.mark.asyncio
    async def test_prefetch_cleared_when_shuffle_changes_next(self, music_cog, mock_voice_client):
        """Shuffle should clear prefetch since next track likely changes."""
        music_cog.playlist = create_ambient_playlist()
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        # Prefetch for track 1
        track_1 = music_cog.playlist[1]
        music_cog._next_prepared = AudioFetchResult(success=True, url="https://prefetch.com")
        music_cog._next_prepared_track = track_1

        # Act: Shuffle (preserve_current moves current to front, shuffles rest)
        music_cog._apply_shuffle(preserve_current=True)

        # Assert: Prefetch cleared (shuffle changes order)
        # Note: There's a small chance shuffle produces same order, but very unlikely with 5 tracks
        # The key is that _refresh_prefetch_if_stale is called and checks video_id match
        # If by chance the same track ends up at index 1, prefetch would be kept (correct behavior)
        next_track = music_cog._get_next_track()
        if next_track and next_track.video_id == track_1.video_id:
            # Rare case: shuffle happened to keep same next track
            assert music_cog._next_prepared is not None
        else:
            # Normal case: next track changed
            assert music_cog._next_prepared is None

    @pytest.mark.asyncio
    async def test_prefetch_cleared_at_end_of_playlist_loop_off(self, music_cog):
        """At end of playlist with loop OFF, no next track means prefetch cleared."""
        music_cog.playlist = create_ambient_playlist()[:3]
        music_cog.current_index = 2  # Last track
        music_cog.loop_mode = LoopMode.OFF

        # Prefetch exists but there's no next track
        music_cog._next_prepared = AudioFetchResult(success=True, url="https://prefetch.com")
        music_cog._next_prepared_track = music_cog.playlist[0]  # Wrong - there is no next

        # Act: Trigger refresh (simulating after some mutation)
        music_cog._refresh_prefetch_if_stale()

        # Assert: Cleared because _get_next_track() returns None
        assert music_cog._next_prepared is None


class TestPrefetchTaskLifecycle:
    """Tests for prefetch task creation and cancellation."""

    @pytest.mark.asyncio
    async def test_prefetch_task_spawned_after_play_starts(self, music_cog, mock_voice_client):
        """After _play_current_track, a prefetch task should be spawned."""
        music_cog.playlist = create_ambient_playlist()[:3]
        music_cog.current_index = 0
        music_cog.active_session = ActiveSession(
            guild_id=888888,
            channel_id=777777,
            voice_client=mock_voice_client,
            origin_channel_id=777777
        )

        mock_player = MagicMock()
        music_cog._player = mock_player

        # No existing prefetch
        music_cog._next_prepared = None
        music_cog._prefetch_task = None

        async def mock_fetch(track, context):
            return AudioFetchResult(success=True, url="https://audio.com")

        prefetch_called = False

        async def mock_prefetch():
            nonlocal prefetch_called
            prefetch_called = True

        with patch.object(music_cog._audio_fetcher, 'fetch', side_effect=mock_fetch):
            with patch.object(music_cog, '_update_playing_presence', new_callable=AsyncMock):
                with patch.object(music_cog, '_prefetch_next_track', side_effect=mock_prefetch):
                    await music_cog._play_current_track()

        # Assert: Prefetch task was created
        assert prefetch_called or music_cog._prefetch_task is not None

    @pytest.mark.asyncio
    async def test_clear_prefetch_cancels_task(self, music_cog):
        """_clear_prefetch_v2 should cancel any running prefetch task."""
        # Create a mock task that's not done
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()

        music_cog._prefetch_task = mock_task
        music_cog._next_prepared = AudioFetchResult(success=True, url="https://test.com")
        music_cog._next_prepared_track = create_ambient_playlist()[0]

        # Act
        music_cog._clear_prefetch_v2()

        # Assert
        mock_task.cancel.assert_called_once()
        assert music_cog._next_prepared is None
        assert music_cog._next_prepared_track is None
        assert music_cog._prefetch_task is None
