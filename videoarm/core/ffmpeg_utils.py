"""Resolve an ffmpeg binary, preferring a system install.

yt-dlp (stream merging) and audio-segment extraction both need ffmpeg. Rather
than require a system-wide install, fall back to the binary bundled with
``imageio-ffmpeg`` when ffmpeg is not on PATH.
"""

import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Return a path to an ffmpeg executable.

    Prefers a system ffmpeg on PATH; otherwise uses the static binary that
    ships with ``imageio-ffmpeg``. Raises RuntimeError if neither is available.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system

    try:
        import imageio_ffmpeg  # noqa: PLC0415

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "ffmpeg is not installed and the bundled imageio-ffmpeg binary "
            "could not be loaded. Install ffmpeg (e.g. `apt-get install ffmpeg`) "
            "or `pip install imageio-ffmpeg`."
        ) from exc


def has_audio_stream(video_path: str) -> bool:
    """Return True if the file contains at least one audio stream.

    Prefers system ``ffprobe`` when present, but ``imageio-ffmpeg`` ships only
    ffmpeg (no ffprobe), so the fallback parses ``ffmpeg -i`` stderr — which
    lists every stream as ``Stream #x:y ...: Audio: ...``. Using the resolved
    ffmpeg binary keeps audio detection working without a system install;
    hard-coding ``ffprobe`` previously made every transcription fail with
    "No such file or directory: 'ffprobe'" and silently dropped the audio.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return bool(probe.stdout.strip())

    # ffmpeg prints stream info to stderr and exits non-zero (no output file
    # requested); that's expected — we only care about the stream listing.
    info = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    return any(
        "Audio:" in line
        for line in info.stderr.splitlines()
        if "Stream #" in line
    )


# Codecs OpenCV's bundled ffmpeg cannot decode in software: its av1 decoder is
# hwaccel-only (no libdav1d), so every cap.read() fails and frame extraction
# would feed the vision model black frames.
_OPENCV_UNDECODABLE = {"av1"}


def video_codec(video_path: str) -> Optional[str]:
    """Return the codec name of the first video stream, or None if unknown."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return probe.stdout.strip() or None

    info = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    for line in info.stderr.splitlines():
        if "Stream #" in line and "Video:" in line:
            # "Stream #0:0 ...: Video: av1 (Main), yuv420p, ..."
            return line.split("Video:")[1].strip().split(" ")[0].rstrip(",")
    return None


def ensure_decodable(video_path: str) -> None:
    """Re-encode the file in place when OpenCV cannot decode its video codec.

    AV1 sources (common for 4K YouTube/CDN files) must be converted before the
    pipeline runs: the system ffmpeg decodes them fine (libdav1d), but OpenCV
    does not, and silently-black frames produce hallucinated visual summaries.
    Height is capped at 720p — frame sampling downsizes to a 256px short side
    anyway — which also keeps the re-encode fast.
    """
    codec = video_codec(video_path)
    if codec not in _OPENCV_UNDECODABLE:
        return

    src = Path(video_path)
    tmp = src.with_name(src.stem + ".h264.tmp.mp4")  # same dir → atomic replace
    print(f"│  Codec  : {codec} not decodable by OpenCV — re-encoding to H.264 …",
          flush=True)
    t0 = time.time()
    try:
        subprocess.run(
            [
                ffmpeg_path(), "-y", "-v", "error", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", "scale=-2:min(720\\,ih)",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", "-f", "mp4", str(tmp),
            ],
            check=True, capture_output=True, text=True, timeout=3 * 3600,
        )
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        tail = (exc.stderr or "").strip()[-500:]
        raise RuntimeError(
            f"Re-encoding {codec} video to H.264 failed: {tail}"
        ) from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, src)
    print(f"│  Codec  : re-encoded to H.264 in {time.time() - t0:.0f}s", flush=True)
