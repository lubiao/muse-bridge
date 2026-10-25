# SPDX-License-Identifier: Apache-2.0
"""Turn a video into a few still frames Muse can actually look at.

Muse's attachment channel passes any media type through untouched, but its image
channel is the only one the model can see into. A forwarded ``.mp4`` therefore
arrives as an opaque blob, while three representative frames let Muse answer
"what is happening in this clip?".

Decoding uses PyAV, which ships its own statically linked ffmpeg libraries. The
Debian ``ffmpeg`` package was measured at +449 MB because it pulls in
``libllvm19`` and ``mesa-libgallium`` through ``libplacebo``; the PyAV wheel adds
about 104 MB and needs nothing from the system. That matters on a NAS.

Frame choice is deliberately crude and cheap: one near the start, one at the
midpoint, one near the end. That covers the setup, the middle and the payoff of a
short clip. Any failure returns no frames so the caller can fall back to
forwarding the original file.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

FRAME_COUNT = 3
FRAME_MAX_WIDTH = 1280
FRAME_QUALITY = 80         # JPEG quality for the re-encode
MAX_FRAME_BYTES = 4 * 1024 * 1024
SEEK_LIMIT_SECONDS = 3600  # refuse to decode an implausibly long video


def available() -> bool:
    """True when the video decoder can be imported."""
    try:
        import av  # noqa: F401
    except Exception:
        return False
    return True


def _positions(duration: float) -> list[float]:
    """Timestamps to sample, avoiding the very first and last frame."""
    if duration <= 0:
        return []
    if duration < 1.5:
        # Too short to divide meaningfully; a single frame is the whole story.
        return [duration / 2]
    return [round(duration * 0.1, 3), round(duration * 0.5, 3),
            round(duration * 0.9, 3)][:FRAME_COUNT]


def duration_seconds(video: Path) -> float | None:
    """Container duration, or None when it cannot be read."""
    try:
        import av
        with av.open(str(video)) as container:
            if container.duration is None:
                return None
            return float(container.duration) / 1_000_000  # microseconds
    except Exception:
        return None


def _frame_to_jpeg(frame) -> bytes | None:
    try:
        from PIL import Image
        width = frame.width
        image = frame.to_image()
        if width > FRAME_MAX_WIDTH:
            height = max(1, round(frame.height * FRAME_MAX_WIDTH / width))
            image = image.resize((FRAME_MAX_WIDTH, height), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=FRAME_QUALITY, optimize=True)
        data = buffer.getvalue()
        return data if 0 < len(data) <= MAX_FRAME_BYTES else None
    except Exception:
        return None


def extract_frames(video: Path) -> list[tuple[bytes, str]]:
    """Return ``[(jpeg_bytes, filename), ...]`` ordered start→end.

    An empty list means extraction is unavailable or the file could not be
    decoded; callers must then forward the original file instead.
    """
    video = Path(video)
    if not video.is_file() or video.stat().st_size <= 0:
        return []
    seconds = duration_seconds(video)
    if seconds is None or seconds <= 0 or seconds > SEEK_LIMIT_SECONDS:
        if seconds is not None:
            LOGGER.warning("video_duration_out_of_range")
        return []
    stops = _positions(seconds)
    if not stops:
        return []
    frames: list[tuple[bytes, str]] = []
    try:
        import av
        with av.open(str(video)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return []
            for index, offset in enumerate(stops):
                try:
                    # Seek to just before the target and decode forward; a key
                    # frame seek alone would land far from the requested moment.
                    container.seek(int(offset * 1_000_000), stream=stream,
                                   backward=True, any_frame=False)
                    frame = None
                    for candidate in container.decode(stream):
                        frame = candidate
                        if candidate.time is not None and candidate.time >= offset:
                            break
                    if frame is None:
                        continue
                    data = _frame_to_jpeg(frame)
                    if data:
                        frames.append((data, "video-frame%d.jpg" % (index + 1)))
                except Exception:
                    # One bad timestamp must not void the frames already read.
                    continue
    except Exception:
        LOGGER.warning("video_decode_unavailable")
        return []
    if not frames:
        LOGGER.warning("video_frames_unavailable")
    return frames


def describe_offset(index: int, total: int, seconds: float) -> str:
    """Human label for a frame, used in the caption Muse receives."""
    if total <= 1:
        label = "画面"
    elif index == 0:
        label = "开头"
    elif index == total - 1:
        label = "结尾"
    else:
        label = "中间"
    return "%s（约 %s）" % (label, _clock(seconds))


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    return "%d:%02d" % (total // 60, total % 60)
