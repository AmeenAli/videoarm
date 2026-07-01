"""
ASR via vllm — HTTP calls to the local vllm ASR server (port 8001).

No in-process model loading. All inference is handled by the vllm server
started with `bash start_servers.sh`. The returned objects mirror the
OpenAI whisper verbose_json interface so the rest of the codebase is
unchanged.
"""

import os
from typing import List, Optional

import requests


class _ASRSegment:
    """Mirrors one segment from OpenAI whisper verbose_json."""
    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end   = end
        self.text  = text


class _ASRResult:
    """Mirrors the OpenAI whisper verbose_json top-level object."""
    __slots__ = ("text", "segments")

    def __init__(self, text: str, segments: List[_ASRSegment]) -> None:
        self.text     = text
        self.segments = segments


def transcribe_audio_local(
    audio_path: str,
    language: Optional[str] = None,
) -> _ASRResult:
    """
    Transcribe a WAV file by calling the vllm ASR server at port 8001.

    Env vars (all optional):
        VIDEOARM_BASE_URL_AUDIO_TRANSCRIBER  default: http://localhost:8001
        VIDEOARM_MODEL_AUDIO_TRANSCRIBER     default: Qwen/Qwen3-ASR-1.7B
        OPENAI_API_KEY                       default: EMPTY

    Returns:
        _ASRResult whose .segments have .start / .end / .text
        — identical interface to the OpenAI whisper verbose_json response.
    """
    base_url = os.getenv(
        "VIDEOARM_BASE_URL_AUDIO_TRANSCRIBER", "http://localhost:8001"
    ).rstrip("/")
    model   = os.getenv("VIDEOARM_MODEL_AUDIO_TRANSCRIBER", "Qwen/Qwen3-ASR-1.7B")
    api_key = os.getenv("OPENAI_API_KEY", "EMPTY")

    url = f"{base_url}/v1/audio/transcriptions"

    # Qwen3-ASR-1.7B only supports "json" or "text"; verbose_json is unsupported.
    form: dict = {"model": model, "response_format": "json"}
    if language:
        form["language"] = language

    with open(audio_path, "rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.wav", fh, "audio/wav")},
            data=form,
            timeout=600,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"ASR server returned {resp.status_code}: {resp.text[:400]}"
        )

    payload   = resp.json()
    full_text = (payload.get("text") or "").strip()

    # "json" format returns only a top-level "text" field — no per-word segments.
    # Wrap the whole transcript as a single segment spanning the entire file.
    raw_segs  = payload.get("segments") or []
    segments = [
        _ASRSegment(
            start=float(s.get("start", 0.0)),
            end=float(s.get("end",   0.0)),
            text=(s.get("text") or "").strip(),
        )
        for s in raw_segs
        if (s.get("text") or "").strip()
    ]

    if not segments and full_text:
        segments = [_ASRSegment(0.0, 0.0, full_text)]

    return _ASRResult(text=full_text, segments=segments)
