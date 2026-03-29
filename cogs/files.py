"""cogs/files.py

This cog contains commands for file manipulation: image resizing, format
conversion (image/audio/video), and user asset fetching (avatars, banners).

Conversion uses PIL for static images and FFmpeg for audio, video, and
animated image formats. A Components V2 view lets users confirm settings
before conversion, with an optional modal for freeform overrides.

Background tasks and network calls are offloaded via ``asyncio.to_thread``
and ``asyncio.create_subprocess_exec`` so the event loop stays responsive.
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import aiohttp
import discord
from discord.ext import commands
from PIL import Image as PILImage

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot

logger = logging.getLogger(__name__)

# =============================================================================
# Format Constants
# =============================================================================

STATIC_IMAGE_FORMATS = {"png", "jpeg", "webp", "bmp", "tiff", "ico"}
ANIMATED_IMAGE_FORMATS = {"gif", "apng", "awebp"}
AUDIO_FORMATS = {"mp3", "wav", "ogg", "flac", "aac", "m4a"}
VIDEO_FORMATS = {"mp4", "webm", "mkv"}

# All recognized target formats (for NLP parsing)
ALL_TARGET_FORMATS = STATIC_IMAGE_FORMATS | ANIMATED_IMAGE_FORMATS | AUDIO_FORMATS | VIDEO_FORMATS

# Extension aliases users might type
FORMAT_ALIASES: Dict[str, str] = {
    "jpg": "jpeg",
}

# Map format -> file extension
FORMAT_TO_EXT: Dict[str, str] = {
    "jpeg": "jpg",
    "awebp": "webp",
    "apng": "apng",
    "m4a": "m4a",
    "aac": "aac",
}


def _category_of(fmt: str) -> str:
    """Returns the category for a format string.

    Args:
        fmt: Lowercase format name.

    Returns:
        One of 'static_image', 'animated_image', 'audio', 'video'.
    """
    if fmt in STATIC_IMAGE_FORMATS:
        return "static_image"
    if fmt in ANIMATED_IMAGE_FORMATS:
        return "animated_image"
    if fmt in AUDIO_FORMATS:
        return "audio"
    if fmt in VIDEO_FORMATS:
        return "video"
    return "unknown"


# =============================================================================
# Conversion Matrix
# =============================================================================

# Allowed (source_category, target_category) pairs
_ALLOWED_CONVERSIONS = {
    ("static_image", "static_image"),
    ("animated_image", "animated_image"),
    ("animated_image", "video"),
    ("audio", "audio"),
    ("audio", "video"),
    ("video", "audio"),
    ("video", "video"),
    ("video", "animated_image"),
}


def is_conversion_allowed(source_cat: str, target_cat: str) -> bool:
    """Check whether a source->target category conversion is supported.

    Args:
        source_cat: Source category string.
        target_cat: Target category string.

    Returns:
        True if the conversion path is in the allowed matrix.
    """
    return (source_cat, target_cat) in _ALLOWED_CONVERSIONS


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ProbeResult:
    """Information extracted from probing a media file.

    Attributes:
        category: 'static_image', 'animated_image', 'audio', or 'video'.
        format_name: Human-readable format (e.g. 'PNG', 'MP4').
        codec: Primary codec name (e.g. 'h264', 'aac').
        width: Pixel width (images/video).
        height: Pixel height (images/video).
        fps: Frames per second (video/animated).
        duration: Duration in seconds.
        bitrate: Overall bitrate in kbps.
        channels: Audio channel count.
        sample_rate: Audio sample rate in Hz.
        file_size: File size in bytes.
        audio_streams: Number of distinct audio streams.
        frame_count: Number of frames (animated images).
        audio_codec: Audio codec if separate from primary.
        video_bitrate: Video-specific bitrate in kbps.
        audio_bitrate: Audio-specific bitrate in kbps.
    """
    category: str
    format_name: str
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    duration: Optional[float] = None
    bitrate: Optional[int] = None
    channels: Optional[int] = None
    sample_rate: Optional[int] = None
    file_size: int = 0
    audio_streams: int = 0
    frame_count: Optional[int] = None
    audio_codec: Optional[str] = None
    video_bitrate: Optional[int] = None
    audio_bitrate: Optional[int] = None

    @property
    def display_info(self) -> str:
        """One-line summary for the V2 view source section.

        Returns:
            Formatted string like 'H.264 | 1920x1080 | 30fps | 2:34'.
        """
        parts: list[str] = []
        if self.codec:
            parts.append(self.codec.upper())
        if self.width and self.height:
            parts.append(f"{self.width}\u00d7{self.height}")
        if self.fps:
            parts.append(f"{self.fps:.0f}fps")
        if self.duration is not None:
            mins, secs = divmod(int(self.duration), 60)
            parts.append(f"{mins}:{secs:02d}")
        if self.bitrate:
            parts.append(f"{self.bitrate}kbps")
        if self.channels:
            ch = "Mono" if self.channels == 1 else "Stereo" if self.channels == 2 else f"{self.channels}ch"
            parts.append(ch)
        if self.sample_rate:
            parts.append(f"{self.sample_rate}Hz")
        return " \u2022 ".join(parts) if parts else self.format_name


@dataclass
class SettingOption:
    """A single option in a dropdown setting.

    Attributes:
        value: Internal value string (e.g. '192').
        label: Display label (e.g. '192kbps (better)').
    """
    value: str
    label: str


@dataclass
class SettingDef:
    """Definition of a configurable conversion parameter.

    Attributes:
        key: Internal key ('bitrate', 'quality', etc.).
        label: User-facing label ('Bitrate').
        freeform: True if the user enters a value via modal text input.
        options: Dropdown options (empty for freeform).
        default: Default value string.
        unit: Unit label ('kbps', 'Hz', 'px', '').
        min_val: Minimum for freeform validation.
        max_val: Maximum for freeform validation.
    """
    key: str
    label: str
    freeform: bool = False
    options: List[SettingOption] = field(default_factory=list)
    default: str = ""
    unit: str = ""
    min_val: Optional[int] = None
    max_val: Optional[int] = None


@dataclass
class ConversionJob:
    """All state for a pending or in-progress conversion.

    Attributes:
        probe: Probe results for the source file.
        target_format: Target format string (lowercase).
        source_category: Category of the source.
        target_category: Category of the target.
        settings: Current setting values keyed by SettingDef.key.
        setting_defs: The available setting definitions for this job.
        source_filename: Original filename from attachment.
        source_bytes: Raw bytes of the source file.
        author_id: Discord user ID who initiated this.
        original_message: The triggering message (for reply context).
    """
    probe: ProbeResult
    target_format: str
    source_category: str
    target_category: str
    settings: Dict[str, str]
    setting_defs: List[SettingDef]
    source_filename: str
    source_bytes: bytes
    author_id: int
    original_message: Optional[discord.Message] = None


# =============================================================================
# Setting Profile Builders
# =============================================================================

# Reusable option sets
_BITRATE_OPTIONS = [
    SettingOption("64", "64kbps (low)"),
    SettingOption("128", "128kbps (good)"),
    SettingOption("192", "192kbps (better)"),
    SettingOption("256", "256kbps (great)"),
    SettingOption("320", "320kbps (best)"),
]

_SAMPLE_RATE_OPTIONS = [
    SettingOption("22050", "22050 Hz (low)"),
    SettingOption("44100", "44100 Hz (CD quality)"),
    SettingOption("48000", "48000 Hz (studio)"),
]

_CHANNEL_OPTIONS = [
    SettingOption("1", "Mono (1 channel)"),
    SettingOption("2", "Stereo (2 channels)"),
]

_RESOLUTION_OPTIONS = [
    SettingOption("original", "Original"),
    SettingOption("1080", "1080p"),
    SettingOption("720", "720p"),
    SettingOption("480", "480p"),
    SettingOption("360", "360p"),
]

_FPS_OPTIONS = [
    SettingOption("10", "10fps"),
    SettingOption("15", "15fps"),
    SettingOption("20", "20fps"),
    SettingOption("24", "24fps"),
    SettingOption("30", "30fps"),
    SettingOption("60", "60fps"),
]


def _build_settings(source_cat: str, target_cat: str, target_fmt: str, probe: ProbeResult) -> List[SettingDef]:
    """Build the list of configurable settings for a given conversion path.

    Args:
        source_cat: Source category.
        target_cat: Target category.
        target_fmt: Target format (lowercase).
        probe: Probe result of the source.

    Returns:
        List of SettingDef for this conversion path.
    """
    defs: List[SettingDef] = []

    if source_cat == "static_image" and target_cat == "static_image":
        default_quality = "85" if target_fmt == "jpeg" else "80" if target_fmt == "webp" else "95"
        defs.append(SettingDef(
            key="quality", label="Quality", freeform=True,
            default=default_quality, unit="", min_val=1, max_val=100,
        ))
        defs.append(SettingDef(
            key="color_mode", label="Color mode", freeform=False,
            options=[
                SettingOption("RGB", "RGB (standard)"),
                SettingOption("RGBA", "RGBA (transparency)"),
                SettingOption("L", "L (grayscale)"),
            ],
            default="RGB",
        ))

    elif target_cat == "audio":
        # Audio->Audio or Video->Audio
        defs.append(SettingDef(
            key="bitrate", label="Bitrate", freeform=False,
            options=_BITRATE_OPTIONS, default="192",
        ))
        defs.append(SettingDef(
            key="sample_rate", label="Sample rate", freeform=False,
            options=_SAMPLE_RATE_OPTIONS, default="44100",
        ))
        defs.append(SettingDef(
            key="channels", label="Channels", freeform=False,
            options=_CHANNEL_OPTIONS, default="2",
        ))
        # Video->Audio extras
        if source_cat == "video":
            if probe.audio_streams > 1:
                stream_opts = [SettingOption(str(i), f"Stream {i}") for i in range(probe.audio_streams)]
                defs.append(SettingDef(
                    key="stream", label="Audio stream", freeform=False,
                    options=stream_opts, default="0",
                ))
            defs.append(SettingDef(
                key="channel_split", label="Channel splitting", freeform=False,
                options=[
                    SettingOption("keep", "Keep as-is"),
                    SettingOption("split", "Split to separate files"),
                ],
                default="keep",
            ))

    elif source_cat in ("video", "animated_image") and target_cat == "animated_image":
        # Video->Animated or Animated->Animated
        fps_opts = list(_FPS_OPTIONS)
        # Cap GIF at 50fps (spec minimum delay ~20ms)
        if target_fmt == "gif":
            fps_opts = [o for o in fps_opts if int(o.value) <= 50]
        defs.append(SettingDef(
            key="fps", label="FPS", freeform=False,
            options=fps_opts, default="15",
        ))
        defs.append(SettingDef(
            key="width", label="Width", freeform=True,
            default="480", unit="px", min_val=50, max_val=1920,
        ))
        if source_cat == "video":
            defs.append(SettingDef(
                key="max_duration", label="Max duration", freeform=True,
                default="10", unit="s", min_val=1, max_val=30,
            ))

    elif source_cat == "animated_image" and target_cat == "video":
        defs.append(SettingDef(
            key="resolution", label="Resolution", freeform=False,
            options=_RESOLUTION_OPTIONS, default="original",
        ))

    elif source_cat == "video" and target_cat == "video":
        defs.append(SettingDef(
            key="resolution", label="Resolution", freeform=False,
            options=_RESOLUTION_OPTIONS, default="original",
        ))
        defs.append(SettingDef(
            key="video_bitrate", label="Video bitrate", freeform=True,
            default="auto", unit="kbps", min_val=100, max_val=10000,
        ))
        defs.append(SettingDef(
            key="audio_bitrate", label="Audio bitrate", freeform=False,
            options=[o for o in _BITRATE_OPTIONS if int(o.value) <= 256],
            default="128",
        ))

    elif source_cat == "audio" and target_cat == "video":
        defs.append(SettingDef(
            key="resolution", label="Resolution", freeform=False,
            options=[o for o in _RESOLUTION_OPTIONS if o.value in ("360", "480", "720")],
            default="480",
        ))
        defs.append(SettingDef(
            key="bg_color", label="Background color", freeform=True,
            default="#1a1a2e", unit="",
        ))

    return defs


def _defaults_from_defs(defs: List[SettingDef]) -> Dict[str, str]:
    """Build a defaults dict from setting definitions.

    Args:
        defs: List of setting definitions.

    Returns:
        Dict mapping setting key to default value.
    """
    return {d.key: d.default for d in defs}


# =============================================================================
# Size Estimation
# =============================================================================

def estimate_output_size(probe: ProbeResult, target_format: str, settings: Dict[str, str]) -> Tuple[float, str]:
    """Estimate the output file size after conversion.

    Args:
        probe: Source file probe result.
        target_format: Target format (lowercase).
        settings: Current conversion settings.

    Returns:
        Tuple of (estimated_mb, confidence) where confidence is
        'low', 'medium', or 'high'.
    """
    target_cat = _category_of(target_format)
    duration = probe.duration or 0.0

    if target_cat == "audio":
        bitrate_kbps = int(settings.get("bitrate", "192"))
        estimated = bitrate_kbps * duration / 8 / 1024
        return (estimated, "high")

    if target_cat == "video":
        if probe.category == "audio":
            # Audio->Video: tiny video stream + audio
            audio_bitrate = probe.bitrate or 192
            video_bitrate = 50  # Nearly nothing for a static frame
            estimated = (audio_bitrate + video_bitrate) * duration / 8 / 1024
            return (estimated, "medium")

        vb_str = settings.get("video_bitrate", "auto")
        video_bitrate = int(vb_str) if vb_str != "auto" else (probe.video_bitrate or 2000)
        audio_bitrate = int(settings.get("audio_bitrate", "128"))
        estimated = (video_bitrate + audio_bitrate) * duration / 8 / 1024
        return (estimated, "medium")

    if target_cat == "animated_image":
        width = int(settings.get("width", "480"))
        fps = int(settings.get("fps", "15"))
        max_dur = float(settings.get("max_duration", str(duration)))
        effective_dur = min(max_dur, duration) if duration > 0 else max_dur
        # Height estimate from aspect ratio
        height = width  # Rough square assumption
        if probe.width and probe.height and probe.width > 0:
            height = int(width * probe.height / probe.width)
        if target_format == "gif":
            # LZW compression is unpredictable; use rough upper bound
            estimated = width * height * fps * effective_dur * 0.3 / 1024 / 1024
        else:
            # APNG and aWebP compress better
            estimated = width * height * fps * effective_dur * 0.15 / 1024 / 1024
        return (estimated, "low")

    if target_cat == "static_image":
        w = probe.width or 1920
        h = probe.height or 1080
        bpp = 3  # bytes per pixel (RGB)
        uncompressed = w * h * bpp
        compression = {"png": 0.4, "jpeg": 0.1, "webp": 0.08, "bmp": 1.0, "tiff": 0.5, "ico": 0.3}
        factor = compression.get(target_format, 0.3)
        estimated = uncompressed * factor / 1024 / 1024
        return (estimated, "low")

    return (probe.file_size / 1024 / 1024, "low")


def format_estimate(estimated_mb: float, confidence: str) -> str:
    """Format a size estimate for display in the V2 view.

    Args:
        estimated_mb: Estimated size in MB.
        confidence: 'low', 'medium', or 'high'.

    Returns:
        Formatted string with warnings if applicable.
    """
    if estimated_mb < 0.01:
        size_str = "<0.01 MB"
    elif estimated_mb < 1:
        size_str = f"~{estimated_mb:.2f} MB"
    else:
        size_str = f"~{estimated_mb:.1f} MB"

    qualifier = "" if confidence == "high" else " (rough estimate)"

    if estimated_mb > 25:
        return f"\u274c {size_str}{qualifier} \u2014 will likely exceed the 25 MB limit"
    if estimated_mb > 20:
        return f"\u26a0\ufe0f {size_str}{qualifier} \u2014 may exceed the 25 MB upload limit"
    return f"{size_str}{qualifier}"


# =============================================================================
# Probe Functions
# =============================================================================

def _get_ffprobe_path() -> str:
    """Get the path to the ffprobe executable.

    Uses the same discovery logic as ffmpeg: bundled path first,
    then system PATH.

    Returns:
        Path to ffprobe executable.
    """
    import shutil

    if getattr(sys, 'frozen', False):
        name = 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe'
        bundled = os.path.join(config.APP_PATH, name)
        if os.path.exists(bundled):
            return bundled

    found = shutil.which('ffprobe')
    if found:
        return found
    return 'ffprobe'


def _get_ffmpeg_path() -> str:
    """Get the path to the ffmpeg executable.

    Reuses musicutils logic if available, otherwise discovers independently.

    Returns:
        Path to ffmpeg executable.
    """
    try:
        from utils.musicutils.music_helpers import get_ffmpeg_path
        return get_ffmpeg_path()
    except ImportError:
        import shutil
        if getattr(sys, 'frozen', False):
            name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
            bundled = os.path.join(config.APP_PATH, name)
            if os.path.exists(bundled):
                return bundled
        found = shutil.which('ffmpeg')
        return found or 'ffmpeg'


def _probe_with_pil(data: bytes) -> Optional[ProbeResult]:
    """Attempt to probe a file using PIL.

    Args:
        data: Raw file bytes.

    Returns:
        ProbeResult if PIL can open the file, None otherwise.
    """
    try:
        img = PILImage.open(io.BytesIO(data))
    except Exception:
        return None

    fmt = (img.format or "UNKNOWN").upper()
    n_frames = getattr(img, 'n_frames', 1)
    is_animated = getattr(img, 'is_animated', False) or n_frames > 1

    # WebP can be animated
    if fmt == "WEBP" and is_animated:
        category = "animated_image"
        fmt_name = "Animated WebP"
    elif fmt == "GIF" and is_animated:
        category = "animated_image"
        fmt_name = "GIF"
    elif fmt == "PNG" and is_animated:
        # APNG detection: PIL may report as PNG with multiple frames
        category = "animated_image"
        fmt_name = "APNG"
    else:
        category = "static_image"
        fmt_name = fmt

    width, height = img.size

    # Estimate fps for animated images
    fps = None
    duration = None
    if is_animated and n_frames > 1:
        try:
            frame_duration = img.info.get('duration', 100)  # ms per frame
            if frame_duration > 0:
                fps = 1000.0 / frame_duration
                duration = n_frames * frame_duration / 1000.0
        except Exception:
            pass

    img.close()

    return ProbeResult(
        category=category,
        format_name=fmt_name,
        codec=fmt.lower(),
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        file_size=len(data),
        frame_count=n_frames if is_animated else None,
    )


async def _probe_with_ffprobe(data: bytes) -> Optional[ProbeResult]:
    """Probe a media file using ffprobe.

    Writes data to a temp file, runs ffprobe, parses JSON output.

    Args:
        data: Raw file bytes.

    Returns:
        ProbeResult if ffprobe succeeded, None otherwise.
    """
    os.makedirs(config.TRANSFORM_CACHE_PATH, exist_ok=True)
    tmp_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"probe_{uuid.uuid4().hex}")

    try:
        await asyncio.to_thread(_write_bytes, tmp_path, data)

        ffprobe = _get_ffprobe_path()
        proc = await asyncio.create_subprocess_exec(
            ffprobe, '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            return None

        info = json.loads(stdout.decode())
        streams = info.get('streams', [])
        fmt_info = info.get('format', {})

        video_streams = [s for s in streams if s.get('codec_type') == 'video']
        audio_streams_list = [s for s in streams if s.get('codec_type') == 'audio']

        has_video = len(video_streams) > 0
        has_audio = len(audio_streams_list) > 0

        if has_video:
            vs = video_streams[0]
            codec = vs.get('codec_name', '')
            width = int(vs.get('width', 0)) or None
            height = int(vs.get('height', 0)) or None
            fps_str = vs.get('r_frame_rate', '0/1')
            try:
                num, den = fps_str.split('/')
                fps = float(num) / float(den) if float(den) != 0 else None
            except (ValueError, ZeroDivisionError):
                fps = None
            v_bitrate = int(vs.get('bit_rate', 0)) // 1000 if vs.get('bit_rate') else None
        else:
            codec = None
            width = height = None
            fps = None
            v_bitrate = None

        duration_str = fmt_info.get('duration')
        duration = float(duration_str) if duration_str else None

        overall_bitrate = int(fmt_info.get('bit_rate', 0)) // 1000 if fmt_info.get('bit_rate') else None

        channels = None
        sample_rate = None
        a_codec = None
        a_bitrate = None
        if has_audio:
            aus = audio_streams_list[0]
            channels = int(aus.get('channels', 0)) or None
            sr = aus.get('sample_rate')
            sample_rate = int(sr) if sr else None
            a_codec = aus.get('codec_name')
            a_bitrate = int(aus.get('bit_rate', 0)) // 1000 if aus.get('bit_rate') else None

        fmt_name_raw = fmt_info.get('format_name', 'unknown')

        # Determine category
        if has_video and has_audio:
            category = "video"
        elif has_video and not has_audio:
            if fmt_name_raw in ('gif', 'apng', 'webp_pipe', 'image2'):
                category = "animated_image"
            else:
                category = "video"
        elif has_audio and not has_video:
            category = "audio"
        else:
            return None

        # Friendly format name
        _FORMAT_NAMES: Dict[str, str] = {
            'mp3': 'MP3', 'wav': 'WAV', 'ogg': 'OGG', 'flac': 'FLAC',
            'aac': 'AAC', 'mp4': 'MP4', 'webm': 'WebM', 'matroska': 'MKV',
            'matroska,webm': 'MKV', 'mov,mp4,m4a,3gp,3g2,mj2': 'MP4',
        }
        display_name = _FORMAT_NAMES.get(fmt_name_raw, fmt_name_raw.upper())

        return ProbeResult(
            category=category,
            format_name=display_name,
            codec=codec or a_codec,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            bitrate=overall_bitrate,
            channels=channels,
            sample_rate=sample_rate,
            file_size=len(data),
            audio_streams=len(audio_streams_list),
            audio_codec=a_codec,
            video_bitrate=v_bitrate,
            audio_bitrate=a_bitrate,
        )

    except asyncio.TimeoutError:
        logger.warning("ffprobe timed out")
        return None
    except Exception as e:
        logger.debug(f"ffprobe failed: {e}")
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def probe_file(data: bytes) -> Optional[ProbeResult]:
    """Probe a file to determine its type and properties.

    Tries PIL first (fast, in-process), falls back to ffprobe for
    audio/video.

    Args:
        data: Raw file bytes.

    Returns:
        ProbeResult or None if unrecognizable.
    """
    result = await asyncio.to_thread(_probe_with_pil, data)
    if result:
        return result
    return await _probe_with_ffprobe(data)


def _write_bytes(path: str, data: bytes) -> None:
    """Write bytes to a file (sync, for use in to_thread).

    Args:
        path: Destination file path.
        data: Bytes to write.
    """
    with open(path, 'wb') as f:
        f.write(data)


def _read_bytes(path: str) -> bytes:
    """Read bytes from a file (sync, for use in to_thread).

    Args:
        path: File path to read.

    Returns:
        Raw file bytes.
    """
    with open(path, 'rb') as f:
        return f.read()


# =============================================================================
# Concurrency Control
# =============================================================================

class ConversionLimiter:
    """Manages per-user and global conversion concurrency.

    Enforces 1 concurrent conversion per user and a configurable (10) global
    limit across all users.
    """

    def __init__(self, max_global: int = 10):
        """Initialize the limiter.

        Args:
            max_global: Maximum concurrent conversions globally.
        """
        self._global_sem = asyncio.Semaphore(max_global)
        self._active_users: set[int] = set()

    async def acquire(self, user_id: int) -> bool:
        """Try to acquire the per-user and global slot.

        Args:
            user_id: Discord user ID.

        Returns:
            True if acquired, False if the user already has a conversion running.
        """
        if user_id in self._active_users:
            return False
        if self._global_sem._value <= 0:
            return False
        await self._global_sem.acquire()
        self._active_users.add(user_id)
        return True

    def release(self, user_id: int) -> None:
        """Release both user and global slots.

        Args:
            user_id: Discord user ID.
        """
        self._active_users.discard(user_id)
        self._global_sem.release()


# Module-level limiter instance
_conversion_limiter = ConversionLimiter(max_global=config.CONVERT_MAX_CONCURRENT)

# =============================================================================
# Conversion Engine
# =============================================================================

# Progress stage messages
_PROGRESS_STAGES = [
    "Converting...",
    "Still working...",
    "Struggling...",
    "Nearly done...",
    "Going golden...",
]


async def _run_ffmpeg(args: List[str], timeout: int = config.CONVERT_FFMPEG_TIMEOUT) -> Tuple[bool, str]:
    """Run an FFmpeg command with timeout and capture output.

    Args:
        args: Full argument list including ffmpeg path.
        timeout: Kill timeout in seconds.

    Returns:
        Tuple of (success, stderr_output).
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0, stderr.decode(errors='replace')
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "Conversion timed out"


async def _convert_static_image(job: ConversionJob) -> List[Tuple[str, bytes]]:
    """Convert a static image using PIL.

    Args:
        job: The conversion job.

    Returns:
        List of (filename, bytes) tuples.
    """
    def _process() -> Tuple[str, bytes]:
        img = PILImage.open(io.BytesIO(job.source_bytes))
        target = job.target_format.upper()

        # Apply color mode
        color_mode = job.settings.get("color_mode", "RGB")
        if color_mode and img.mode != color_mode:
            # Can't convert to RGBA if target doesn't support it
            if target in ('JPEG', 'BMP') and color_mode == 'RGBA':
                color_mode = 'RGB'
            img = img.convert(color_mode)

        # Handle transparency for formats that don't support it
        if target in ('JPEG', 'BMP') and img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')

        buf = io.BytesIO()
        save_kwargs: Dict[str, Any] = {'format': target}

        quality = job.settings.get("quality")
        if quality and quality.isdigit():
            if target in ('JPEG', 'WEBP'):
                save_kwargs['quality'] = int(quality)
            elif target == 'PNG':
                save_kwargs['compress_level'] = max(0, min(9, 9 - int(int(quality) * 9 / 100)))

        img.save(buf, **save_kwargs)
        buf.seek(0)

        ext = FORMAT_TO_EXT.get(job.target_format, job.target_format)
        base = job.source_filename.rsplit('.', 1)[0]
        filename = f"{base}.{ext}"

        img.close()
        return filename, buf.read()

    result = await asyncio.to_thread(_process)
    return [result]


async def _convert_with_ffmpeg(job: ConversionJob) -> List[Tuple[str, bytes]]:
    """Convert audio/video/animated using FFmpeg.

    Args:
        job: The conversion job.

    Returns:
        List of (filename, bytes) tuples.
    """
    os.makedirs(config.TRANSFORM_CACHE_PATH, exist_ok=True)
    job_id = uuid.uuid4().hex
    input_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"in_{job_id}")

    ext = FORMAT_TO_EXT.get(job.target_format, job.target_format)
    base = job.source_filename.rsplit('.', 1)[0]

    channel_split = job.settings.get("channel_split", "keep") == "split"
    output_paths: List[Tuple[str, str]] = []  # (path, filename)

    try:
        await asyncio.to_thread(_write_bytes, input_path, job.source_bytes)
        ffmpeg = _get_ffmpeg_path()

        args = [ffmpeg, '-y', '-i', input_path]

        if job.target_category == "audio":
            if channel_split and job.probe.channels and job.probe.channels > 1:
                results = await _split_channels(ffmpeg, input_path, base, ext, job)
                return results

            out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"out_{job_id}.{ext}")
            output_paths.append((out_path, f"{base}.{ext}"))

            if job.source_category == "video":
                args.append('-vn')
                stream_idx = job.settings.get("stream", "0")
                args.extend(['-map', f'0:a:{stream_idx}'])

            bitrate = job.settings.get("bitrate", "192")
            sample_rate = job.settings.get("sample_rate", "44100")
            channels = job.settings.get("channels", "2")
            args.extend(['-b:a', f'{bitrate}k', '-ar', sample_rate, '-ac', channels])
            args.append(out_path)

        elif job.source_category == "audio" and job.target_category == "video":
            out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"out_{job_id}.{ext}")
            output_paths.append((out_path, f"{base}.{ext}"))

            resolution = job.settings.get("resolution", "480")
            res_map = {"360": "640x360", "480": "854x480", "720": "1280x720"}
            res_str = res_map.get(resolution, "854x480")

            bg_color = job.settings.get("bg_color", "#1a1a2e")
            if not re.match(r'^#[0-9a-fA-F]{6}$', bg_color):
                bg_color = "#1a1a2e"

            args = [
                ffmpeg, '-y',
                '-f', 'lavfi', '-i', f'color=c={bg_color}:s={res_str}:r=1',
                '-i', input_path,
                '-shortest',
                '-c:v', 'libx264', '-tune', 'stillimage',
                '-c:a', 'aac',
                out_path,
            ]

        elif job.target_category == "animated_image":
            actual_ext = "webp" if job.target_format == "awebp" else ext
            out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"out_{job_id}.{actual_ext}")
            output_paths.append((out_path, f"{base}.{actual_ext}"))

            fps = job.settings.get("fps", "15")
            width = job.settings.get("width", "480")
            max_dur = job.settings.get("max_duration")

            if max_dur:
                args.extend(['-t', max_dur])

            if job.target_format == "gif":
                vf = f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
                args.extend(['-vf', vf, '-an', out_path])
            elif job.target_format == "apng":
                vf = f"fps={fps},scale={width}:-1"
                args.extend(['-vf', vf, '-plays', '0', '-an', out_path])
            else:  # awebp
                vf = f"fps={fps},scale={width}:-1"
                args.extend(['-vf', vf, '-loop', '0', '-an', out_path])

        elif job.source_category == "animated_image" and job.target_category == "video":
            out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"out_{job_id}.{ext}")
            output_paths.append((out_path, f"{base}.{ext}"))

            resolution = job.settings.get("resolution", "original")
            vf_parts = []
            if resolution != "original":
                vf_parts.append(f"scale=-2:{resolution}")
            vf_parts.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")

            args.extend(['-movflags', 'faststart', '-pix_fmt', 'yuv420p'])
            if vf_parts:
                args.extend(['-vf', ','.join(vf_parts)])
            args.append(out_path)

        elif job.source_category == "video" and job.target_category == "video":
            out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"out_{job_id}.{ext}")
            output_paths.append((out_path, f"{base}.{ext}"))

            resolution = job.settings.get("resolution", "original")
            v_bitrate = job.settings.get("video_bitrate", "auto")
            a_bitrate = job.settings.get("audio_bitrate", "128")

            if resolution != "original":
                args.extend(['-vf', f'scale=-2:{resolution}'])

            if v_bitrate != "auto":
                args.extend(['-b:v', f'{v_bitrate}k'])
            else:
                args.extend(['-crf', '23'])

            args.extend(['-b:a', f'{a_bitrate}k'])
            args.append(out_path)

        else:
            return []

        success, stderr = await _run_ffmpeg(args)

        if not success:
            # No exc_info: this is an external process failure, not a caught exception. stderr IS the diagnostic.
            logger.error(f"FFmpeg conversion failed: {stderr[:500]}")
            return []

        results: List[Tuple[str, bytes]] = []
        for out_path, filename in output_paths:
            if os.path.exists(out_path):
                out_data = await asyncio.to_thread(_read_bytes, out_path)
                results.append((filename, out_data))

        return results

    finally:
        for path in [input_path] + [p for p, _ in output_paths]:
            try:
                os.remove(path)
            except OSError:
                pass


async def _split_channels(ffmpeg: str, input_path: str, base: str, ext: str, job: ConversionJob) -> List[Tuple[str, bytes]]:
    """Split audio channels into separate files.

    Args:
        ffmpeg: Path to ffmpeg executable.
        input_path: Path to the input file.
        base: Base filename without extension.
        ext: Target file extension.
        job: The conversion job.

    Returns:
        List of (filename, bytes) tuples.
    """
    channels = job.probe.channels or 2
    job_id = uuid.uuid4().hex

    if channels == 2:
        layout = "stereo"
        channel_names = ["left", "right"]
    elif channels == 1:
        return []
    else:
        layout = f"{channels}.0"
        channel_names = [f"ch{i}" for i in range(channels)]

    output_paths: List[Tuple[str, str]] = []

    filter_parts = []
    for i, name in enumerate(channel_names):
        out_path = os.path.join(config.TRANSFORM_CACHE_PATH, f"split_{job_id}_{name}.{ext}")
        output_paths.append((out_path, f"{base}_{name}.{ext}"))
        filter_parts.append(f"[ch{i}]")

    filter_expr = f"channelsplit=channel_layout={layout}" + "".join(filter_parts)

    args = [ffmpeg, '-y', '-i', input_path, '-filter_complex', filter_expr]
    for i, (out_path, _) in enumerate(output_paths):
        args.extend(['-map', f'[ch{i}]', out_path])

    success, stderr = await _run_ffmpeg(args)
    if not success:
        # No exc_info: this is an external process failure, not a caught exception. stderr IS the diagnostic.
        logger.error(f"Channel split failed: {stderr[:500]}")
        return []

    results: List[Tuple[str, bytes]] = []
    try:
        for out_path, filename in output_paths:
            if os.path.exists(out_path):
                data = await asyncio.to_thread(_read_bytes, out_path)
                results.append((filename, data))
    finally:
        for out_path, _ in output_paths:
            try:
                os.remove(out_path)
            except OSError:
                pass

    return results


async def run_conversion(job: ConversionJob) -> List[Tuple[str, bytes]]:
    """Execute a conversion job.

    Dispatches to the appropriate engine based on the conversion path.

    Args:
        job: The conversion job to execute.

    Returns:
        List of (filename, bytes) tuples for the output files.
    """
    if job.source_category == "static_image" and job.target_category == "static_image":
        return await _convert_static_image(job)
    return await _convert_with_ffmpeg(job)


# =============================================================================
# User Asset Dataclass
# =============================================================================

@dataclass
class UserAsset:
    """Represents a fetched user asset (avatar or banner).

    Attributes:
        url: The CDN URL of the asset.
        image_bytes: The raw image data as bytes (None if not yet fetched).
        source: A description of where this asset came from (e.g., 'Global', 'Server').
    """
    url: str
    image_bytes: Optional[bytes] = None
    source: str = "Unknown"

    async def fetch(self, session: aiohttp.ClientSession) -> bytes:
        """Fetches the image bytes from the URL.

        Args:
            session: An aiohttp client session to use for the request.

        Returns:
            The raw image data as bytes.

        Raises:
            aiohttp.ClientError: If the fetch fails.
        """
        async with session.get(self.url) as response:
            response.raise_for_status()
            self.image_bytes = await response.read()
            return self.image_bytes

    def to_pil_image(self) -> PILImage.Image:
        """Converts the fetched bytes to a PIL Image.

        Returns:
            A PIL Image object.

        Raises:
            ValueError: If image_bytes has not been fetched yet.
        """
        if self.image_bytes is None:
            raise ValueError("Image bytes not fetched. Call fetch() first.")
        return PILImage.open(io.BytesIO(self.image_bytes))


# =============================================================================
# Cog
# =============================================================================

class FilesCog(BaseCog):
    """A cog for handling file manipulation commands."""

    def __init__(self, bot: CoreBot):
        """Initializes the FilesCog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)

    # -------------------------------------------------------------------------
    # User Asset Helpers (Reusable for other operations)
    # -------------------------------------------------------------------------

    async def get_avatar(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> UserAsset:
        """Gets a user's global avatar as a UserAsset.

        Args:
            user: The user or member to get the avatar for.
            size: The size of the avatar to fetch (default 1024).

        Returns:
            A UserAsset containing the global avatar URL.
        """
        avatar = user.avatar or user.default_avatar
        url = avatar.replace(size=size, format='png').url
        return UserAsset(url=url, source="Global")

    async def get_guild_avatar(
        self,
        member: discord.Member,
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a member's guild-specific avatar as a UserAsset.

        Args:
            member: The member to get the guild avatar for.
            size: The size of the avatar to fetch (default 1024).

        Returns:
            A UserAsset containing the guild avatar URL, or None if not set.
        """
        if member.guild_avatar is None:
            return None
        url = member.guild_avatar.replace(size=size, format='png').url
        return UserAsset(url=url, source="Server")

    async def get_all_avatars(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> list[UserAsset]:
        """Gets all available avatars (global and guild) for a user.

        Args:
            user: The user or member to get avatars for.
            size: The size of the avatars to fetch (default 1024).

        Returns:
            A list of UserAsset objects for each available avatar.
        """
        assets = [await self.get_avatar(user, size=size)]

        if isinstance(user, discord.Member):
            guild_avatar = await self.get_guild_avatar(user, size=size)
            if guild_avatar:
                assets.append(guild_avatar)

        return assets

    async def get_banner(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a user's global banner as a UserAsset.

        Note: Requires fetching the full user object to access banner data.

        Args:
            user: The user or member to get the banner for.
            size: The size of the banner to fetch (default 1024).

        Returns:
            A UserAsset containing the global banner URL, or None if not set.
        """
        try:
            fetched_user = await self.bot.fetch_user(user.id)
        except discord.HTTPException:
            return None

        if fetched_user.banner is None:
            return None

        fmt = 'gif' if fetched_user.banner.is_animated() else 'png'
        url = fetched_user.banner.replace(size=size, format=fmt).url
        return UserAsset(url=url, source="Global")

    async def get_guild_banner(
        self,
        member: discord.Member,
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a member's guild-specific banner as a UserAsset.

        Args:
            member: The member to get the guild banner for.
            size: The size of the banner to fetch (default 1024).

        Returns:
            A UserAsset containing the guild banner URL, or None if not set.
        """
        try:
            fetched_member = await member.guild.fetch_member(member.id)
        except discord.HTTPException:
            return None

        if not hasattr(fetched_member, 'guild_banner') or fetched_member.guild_banner is None:
            return None

        fmt = 'gif' if fetched_member.guild_banner.is_animated() else 'png'
        url = fetched_member.guild_banner.replace(size=size, format=fmt).url
        return UserAsset(url=url, source="Server")

    async def get_all_banners(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> list[UserAsset]:
        """Gets all available banners (global and guild) for a user.

        Args:
            user: The user or member to get banners for.
            size: The size of the banners to fetch (default 1024).

        Returns:
            A list of UserAsset objects for each available banner.
        """
        assets = []

        global_banner = await self.get_banner(user, size=size)
        if global_banner:
            assets.append(global_banner)

        if isinstance(user, discord.Member):
            guild_banner = await self.get_guild_banner(user, size=size)
            if guild_banner:
                assets.append(guild_banner)

        return assets

    async def fetch_asset_bytes(self, asset: UserAsset) -> UserAsset:
        """Fetches the image bytes for a UserAsset.

        Args:
            asset: The UserAsset to fetch bytes for.

        Returns:
            The same UserAsset with image_bytes populated.
        """
        async with aiohttp.ClientSession() as session:
            await asset.fetch(session)
        return asset

    async def fetch_all_asset_bytes(self, assets: list[UserAsset]) -> list[UserAsset]:
        """Fetches the image bytes for multiple UserAssets concurrently.

        Args:
            assets: The list of UserAssets to fetch bytes for.

        Returns:
            The same list of UserAssets with image_bytes populated.
        """
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*[asset.fetch(session) for asset in assets])
        return assets

    def _extract_user_from_query(self, ctx: commands.Context, query: str) -> Optional[discord.Member]:
        """Extracts a mentioned user from the query string.

        Args:
            ctx: The command context.
            query: The user's input string.

        Returns:
            The mentioned Member, or None if no valid mention found.
        """
        if ctx.message.mentions:
            return ctx.message.mentions[0] if isinstance(ctx.message.mentions[0], discord.Member) else None

        user_id_match = re.search(r'(\d{17,19})', query)
        if user_id_match and ctx.guild:
            try:
                return ctx.guild.get_member(int(user_id_match.group(1)))
            except (ValueError, AttributeError):
                pass

        return None

    # -------------------------------------------------------------------------
    # Media Attachment Discovery
    # -------------------------------------------------------------------------

    async def _find_media_attachments(self, message: discord.Message) -> list[discord.Attachment]:
        """Finds all media attachments in the message or its reply context.

        Checks for images, audio, and video attachments. Falls back to the
        replied-to message if the current message has no media.

        Args:
            message: The message to check.

        Returns:
            A list of media attachments found, which may be empty.
        """
        def _is_media(a: discord.Attachment) -> bool:
            if a.content_type:
                return any(a.content_type.startswith(t) for t in ('image/', 'audio/', 'video/'))
            ext = a.filename.rsplit('.', 1)[-1].lower() if '.' in a.filename else ''
            return ext in ALL_TARGET_FORMATS or ext in FORMAT_ALIASES

        attachments = [a for a in message.attachments if _is_media(a)]
        if attachments:
            return attachments

        if message.reference and isinstance(message.reference.resolved, discord.Message):
            return [a for a in message.reference.resolved.attachments if _is_media(a)]

        return []

    async def _find_image_attachments(self, message: discord.Message) -> list[discord.Attachment]:
        """Finds all valid image attachments in the message or its reply context.

        Args:
            message: The message to check.

        Returns:
            A list of image attachments found, which may be empty.
        """
        attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith('image/')]
        if attachments:
            return attachments

        if message.reference and isinstance(message.reference.resolved, discord.Message):
            return [a for a in message.reference.resolved.attachments if a.content_type and a.content_type.startswith('image/')]

        return []

    # -------------------------------------------------------------------------
    # NLP Handlers for User Assets
    # -------------------------------------------------------------------------

    async def pfp(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for fetching a user's profile picture(s).

        Returns both global and guild-specific avatars if available.

        Args:
            ctx: The command context.
            query: The user's input string (should contain a mention or user ID).
        """
        target = self._extract_user_from_query(ctx, query)
        if target is None:
            if ctx.guild:
                target = ctx.guild.get_member(ctx.author.id)
            if target is None:
                await ctx.send("Please mention a user or provide their ID to fetch their profile picture.")
                return

        try:
            async with ctx.typing():
                avatars = await self.get_all_avatars(target, size=1024)

                embeds = []
                for avatar in avatars:
                    embed = discord.Embed(
                        title=f"{target.display_name}'s {avatar.source} Avatar",
                        color=target.color if hasattr(target, 'color') else discord.Color.blurple()
                    )
                    embed.set_image(url=avatar.url)
                    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    embeds.append(embed)

                if len(avatars) == 1:
                    await ctx.send(embed=embeds[0])
                else:
                    await ctx.send(content=f"Here are {target.display_name}'s avatars:", embeds=embeds)
        except Exception as e:
            self.logger.error(f"Failed to fetch avatar for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to fetch that profile picture.")

    async def banner(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for fetching a user's banner(s).

        Returns both global and guild-specific banners if available.

        Args:
            ctx: The command context.
            query: The user's input string (should contain a mention or user ID).
        """
        target = self._extract_user_from_query(ctx, query)
        if target is None:
            if ctx.guild:
                target = ctx.guild.get_member(ctx.author.id)
            if target is None:
                await ctx.send("Please mention a user or provide their ID to fetch their banner.")
                return

        try:
            async with ctx.typing():
                banners = await self.get_all_banners(target, size=1024)

                if not banners:
                    await ctx.send(f"{target.display_name} doesn't have any banners set.")
                    return

                embeds = []
                for banner_asset in banners:
                    embed = discord.Embed(
                        title=f"{target.display_name}'s {banner_asset.source} Banner",
                        color=target.color if hasattr(target, 'color') else discord.Color.blurple()
                    )
                    embed.set_image(url=banner_asset.url)
                    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    embeds.append(embed)

                if len(banners) == 1:
                    await ctx.send(embed=embeds[0])
                else:
                    await ctx.send(content=f"Here are {target.display_name}'s banners:", embeds=embeds)
        except Exception as e:
            self.logger.error(f"Failed to fetch banner for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to fetch that banner.")

    # -------------------------------------------------------------------------
    # NLP Handler: Resize
    # -------------------------------------------------------------------------

    async def resize(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for resizing an image.

        Parses dimensions (e.g., '500x500') from the query and resizes the
        attached or replied-to image.

        Args:
            ctx: The command context.
            query: The user's input string containing dimensions.
        """
        match = re.search(r'(\d+)\s*x\s*(\d+)', query)
        if not match:
            await ctx.send("I couldn't find the dimensions. Please specify the size like `500x500`.")
            return

        new_size = (int(match.group(1)), int(match.group(2)))
        if not (0 < new_size[0] <= 4000 and 0 < new_size[1] <= 4000):
            await ctx.send("Invalid dimensions. Both width and height must be between 1 and 4000 pixels.")
            return

        attachments = await self._find_image_attachments(ctx.message)
        if not attachments:
            await ctx.send("Please attach an image or reply to a message with an image to resize.")
            return

        def _processing_thread(image_bytes: bytes, size: Tuple[int, int]) -> io.BytesIO:
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                original_format = img.format or 'PNG'
                img = img.resize(size)
                buffer = io.BytesIO()
                img.save(buffer, format=original_format)
                buffer.seek(0)
                return buffer

        try:
            async with ctx.typing():
                all_bytes = await asyncio.gather(*[a.read() for a in attachments])
                buffers = await asyncio.gather(*[
                    asyncio.to_thread(_processing_thread, img_bytes, new_size)
                    for img_bytes in all_bytes
                ])
                files = [
                    discord.File(buf, filename=f"resized_{att.filename}")
                    for att, buf in zip(attachments, buffers, strict=True)
                ]
                label = f"{new_size[0]}x{new_size[1]}"
                if len(files) == 1:
                    await ctx.send(f"Here is the image resized to {label}:", file=files[0])
                else:
                    await ctx.send(f"Here are {len(files)} images resized to {label}:", files=files)
                self.logger.info(f"Resized {len(files)} image(s) to {label} for user {ctx.author.id}")
        except Exception as e:
            self.logger.error(f"Failed to resize image for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to resize that image.")

    # -------------------------------------------------------------------------
    # NLP Handler: Convert (V2 UI)
    # -------------------------------------------------------------------------

    async def convert(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for converting a file's format.

        Probes the attached file, determines the conversion path, and presents
        a Components V2 view with settings confirmation before converting.

        Args:
            ctx: The command context.
            query: The user's input string containing the target format.
        """
        # Normalize aliases
        normalized = query.lower()
        for alias, canonical in FORMAT_ALIASES.items():
            normalized = re.sub(rf'\b{re.escape(alias)}\b', canonical, normalized)

        # Find the target format (longest match first to avoid partial matches)
        all_sortable = sorted(ALL_TARGET_FORMATS, key=len, reverse=True)
        found_formats: set[str] = set()
        for fmt in all_sortable:
            if re.search(rf'\b{re.escape(fmt)}\b', normalized):
                found_formats.add(fmt)

        if len(found_formats) > 1:
            await ctx.send("I found multiple formats in your request. Please specify just one target format.")
            return
        if not found_formats:
            formats_list = ', '.join(sorted(ALL_TARGET_FORMATS))
            await ctx.send(f"I couldn't figure out what format to convert to. Supported formats: `{formats_list}`")
            return

        target_format = found_formats.pop()

        # Find attachments
        attachments = await self._find_media_attachments(ctx.message)
        if not attachments:
            await ctx.send("Please attach a file or reply to a message with an attachment to convert.")
            return

        # Convert only the first attachment
        attachment = attachments[0]

        # Check file size limit
        max_bytes = config.CONVERT_MAX_FILE_SIZE_MB * 1024 * 1024
        if attachment.size > max_bytes:
            await ctx.send(f"That file is too large ({attachment.size / 1024 / 1024:.1f} MB). Maximum is {config.CONVERT_MAX_FILE_SIZE_MB} MB.")
            return

        async with ctx.typing():
            try:
                file_bytes = await attachment.read()
            except Exception as e:
                self.logger.error(f"Failed to read attachment from user {ctx.author.id}: {e}", exc_info=True)
                await ctx.send("I couldn't download that file. Please try again.")
                return

            probe = await probe_file(file_bytes)
            if probe is None:
                await ctx.send("I couldn't identify the file format. Make sure it's a valid image, audio, or video file.")
                return

            source_cat = probe.category
            target_cat = _category_of(target_format)

            if not is_conversion_allowed(source_cat, target_cat):
                self.logger.warning(f"Conversion rejected for user {ctx.author.id}: {source_cat} → {target_cat} not allowed")
                await ctx.send(
                    f"I can't convert a **{source_cat.replace('_', ' ')}** to a **{target_cat.replace('_', ' ')}**. "
                    f"That conversion path isn't supported."
                )
                return

            # Validate duration limits
            if probe.duration is not None:
                if source_cat == "video" and probe.duration > config.CONVERT_MAX_VIDEO_DURATION:
                    mins = config.CONVERT_MAX_VIDEO_DURATION // 60
                    self.logger.warning(f"Conversion rejected for user {ctx.author.id}: video too long ({probe.duration / 60:.1f} min, max {mins} min)")
                    await ctx.send(f"That video is too long ({probe.duration / 60:.1f} min). Maximum is {mins} minutes.")
                    return
                if source_cat == "audio" and probe.duration > config.CONVERT_MAX_AUDIO_DURATION:
                    mins = config.CONVERT_MAX_AUDIO_DURATION // 60
                    self.logger.warning(f"Conversion rejected for user {ctx.author.id}: audio too long ({probe.duration / 60:.1f} min, max {mins} min)")
                    await ctx.send(f"That audio is too long ({probe.duration / 60:.1f} min). Maximum is {mins} minutes.")
                    return

            # Build settings
            setting_defs = _build_settings(source_cat, target_cat, target_format, probe)
            defaults = _defaults_from_defs(setting_defs)

            job = ConversionJob(
                probe=probe,
                target_format=target_format,
                source_category=source_cat,
                target_category=target_cat,
                settings=defaults,
                setting_defs=setting_defs,
                source_filename=attachment.filename,
                source_bytes=file_bytes,
                author_id=ctx.author.id,
                original_message=ctx.message,
            )

            self.logger.info(f"Conversion job created: user {ctx.author.id}, {probe.format_name} → {target_format}, {attachment.size / 1024 / 1024:.1f} MB")
            from utils.views import show_conversion
            await show_conversion(ctx, job, self._execute_conversion)

    async def _execute_conversion(
        self,
        job: ConversionJob,
        update_status: Callable[[str], Awaitable[None]],
    ) -> Tuple[bool, List[Tuple[str, bytes]], str]:
        """Execute a conversion job with progress updates.

        Called by the ConversionView when the user clicks Convert.

        Args:
            job: The conversion job to execute.
            update_status: Callback to update the status text in the V2 view.

        Returns:
            Tuple of (success, output_files, message).
            output_files is a list of (filename, bytes) tuples.
        """
        if not await _conversion_limiter.acquire(job.author_id):
            self.logger.debug(f"Conversion concurrency blocked for user {job.author_id} (already has a conversion running)")
            return False, [], "You already have a conversion running. Please wait for it to finish."

        start_time = time.monotonic()
        progress_task: Optional[asyncio.Task[None]] = None

        try:
            # Start progress updater
            async def _update_progress() -> None:
                stage = 0
                while True:
                    await asyncio.sleep(15)
                    stage += 1
                    if stage <= len(_PROGRESS_STAGES) - 1:
                        await update_status(_PROGRESS_STAGES[stage])
                    else:
                        elapsed = time.monotonic() - start_time
                        await update_status(f"Still going... ({elapsed:.0f}s)")

            progress_task = asyncio.create_task(_update_progress())

            await update_status(_PROGRESS_STAGES[0])

            results = await run_conversion(job)

            if not results:
                self.logger.warning(f"Conversion engine returned no output for user {job.author_id}, {job.source_category} → {job.target_format} (source: {job.source_filename!r})")
                return False, [], "Conversion failed. The file may be corrupt or unsupported."

            for filename, data in results:
                size_mb = len(data) / 1024 / 1024
                if size_mb > config.CONVERT_MAX_FILE_SIZE_MB:
                    return False, [], (
                        f"Output file `{filename}` is {size_mb:.1f} MB, which exceeds the "
                        f"{config.CONVERT_MAX_FILE_SIZE_MB} MB limit. Try reducing quality or resolution."
                    )

            elapsed = time.monotonic() - start_time
            self.logger.info(f"Conversion complete: user {job.author_id}, {job.source_category} → {job.target_format}, {len(results)} file(s), {elapsed:.1f}s")
            return True, results, f"Conversion complete (took {elapsed:.1f}s)"

        except Exception as e:
            self.logger.error(f"Conversion failed: {e}", exc_info=True)
            return False, [], f"An unexpected error occurred: {e}"

        finally:
            if progress_task and not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            _conversion_limiter.release(job.author_id)


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    os.makedirs(config.TRANSFORM_CACHE_PATH, exist_ok=True)
    await bot.add_cog(FilesCog(bot))
