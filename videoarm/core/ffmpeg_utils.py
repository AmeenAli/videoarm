"""Resolve an ffmpeg binary, preferring a system install.

yt-dlp (stream merging) and audio-segment extraction both need ffmpeg. Rather
than require a system-wide install, fall back to the binary bundled with
``imageio-ffmpeg`` when ffmpeg is not on PATH.
"""

import shutil
import subprocess
from functools import lru_cache


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
