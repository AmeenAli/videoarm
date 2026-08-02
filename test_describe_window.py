"""
Tests for the semantic-timeline resolution knob (POST /v1/describe window_secs).

Pure unit tests: window planning, ASR-chunk planning, utterance attribution,
document assembly and request validation. No GPU, no model servers, no video —
run with `python test_describe_window.py` or `pytest test_describe_window.py`.
"""

import sys

from pydantic import ValidationError

from videoarm.api.describe import DescribeRequest
from videoarm.config.settings import (
    DEFAULT_WINDOW_SECS,
    MAX_WINDOW_SECS,
    MIN_ASR_CHUNK_SECS,
    MIN_WINDOW_SECS,
)
from videoarm.core.describer import (
    SemanticDescriber,
    _assemble_document,
    _utterances_for_window,
)

FPS = 24.0
DURATION = 600.0  # 10 minutes
INFO = {"fps": FPS, "total_frames": int(DURATION * FPS)}


def _describer():
    """A SemanticDescriber without touching __init__ (which loads the agent)."""
    return SemanticDescriber.__new__(SemanticDescriber)


# --------------------------------------------------------------------------- #
# Window planning — the resolution must actually reach the timeline
# --------------------------------------------------------------------------- #

def test_window_count_matches_resolution():
    for window_secs in (MIN_WINDOW_SECS, 5, 30, DEFAULT_WINDOW_SECS, MAX_WINDOW_SECS):
        windows = _describer()._plan_windows(INFO, window_secs)
        assert windows, f"no windows planned at {window_secs}s"
        expected = round(DURATION / window_secs)
        assert abs(len(windows) - expected) <= 1, (
            f"{window_secs}s → {len(windows)} windows, expected ~{expected}"
        )


def test_windows_tile_contiguously_at_every_resolution():
    for window_secs in (MIN_WINDOW_SECS, 7, DEFAULT_WINDOW_SECS, MAX_WINDOW_SECS):
        windows = _describer()._plan_windows(INFO, window_secs)
        assert windows[0]["start_time"] == 0.0
        for prev, nxt in zip(windows, windows[1:]):
            assert abs(prev["end_time"] - nxt["start_time"]) < 1e-6, (
                f"gap/overlap at {window_secs}s: {prev['end_time']} → {nxt['start_time']}"
            )
        assert abs(windows[-1]["end_time"] - DURATION) < 1e-6, (
            f"timeline ends at {windows[-1]['end_time']}, video is {DURATION}s"
        )
        # Every window is the requested length; only the last may absorb a
        # short remainder (up to 1.25x).
        for w in windows[:-1]:
            assert abs(w["duration"] - window_secs) < 1.0 / FPS + 1e-6
        assert windows[-1]["duration"] <= window_secs * 1.25 + 1e-6


def test_no_degenerate_trailing_window():
    # 100.1s at 10s windows leaves a 0.1s remainder — it must be folded in.
    info = {"fps": FPS, "total_frames": int(100.1 * FPS)}
    windows = _describer()._plan_windows(info, 10)
    assert windows[-1]["duration"] >= 2.5, (
        f"trailing sliver of {windows[-1]['duration']}s was not merged"
    )


def test_single_window_video_shorter_than_resolution():
    info = {"fps": FPS, "total_frames": int(4 * FPS)}
    windows = _describer()._plan_windows(info, MAX_WINDOW_SECS)
    assert len(windows) == 1
    assert abs(windows[0]["end_time"] - 4.0) < 1e-6


# --------------------------------------------------------------------------- #
# ASR chunking — decoupled from, but aligned to, the timeline
# --------------------------------------------------------------------------- #

def test_asr_chunks_align_to_windows_and_respect_minimum():
    d = _describer()
    for window_secs in (MIN_WINDOW_SECS, 3, 10, DEFAULT_WINDOW_SECS, MAX_WINDOW_SECS):
        windows = d._plan_windows(INFO, window_secs)
        chunks  = d._plan_asr_chunks(windows, window_secs)
        assert chunks
        span = chunks[0]["end_time"] - chunks[0]["start_time"]
        assert span >= min(MIN_ASR_CHUNK_SECS, DURATION) - 1e-6, (
            f"{window_secs}s → {span}s ASR chunk, below the {MIN_ASR_CHUNK_SECS}s floor"
        )
        # Chunk boundaries must fall on window boundaries.
        starts = {round(w["start_time"], 6) for w in windows}
        for c in chunks:
            assert round(c["start_time"], 6) in starts
        # Chunks cover the whole video without gaps.
        assert chunks[0]["start_time"] == 0.0
        assert abs(chunks[-1]["end_time"] - DURATION) < 1e-6


def test_coarse_resolution_keeps_one_chunk_per_window():
    d = _describer()
    windows = d._plan_windows(INFO, DEFAULT_WINDOW_SECS)
    chunks  = d._plan_asr_chunks(windows, DEFAULT_WINDOW_SECS)
    assert len(chunks) == len(windows), "default resolution changed ASR behaviour"


def test_fine_resolution_shares_chunks_across_windows():
    d = _describer()
    windows = d._plan_windows(INFO, MIN_WINDOW_SECS)
    chunks  = d._plan_asr_chunks(windows, MIN_WINDOW_SECS)
    assert len(chunks) < len(windows), (
        "1s windows should share coarser ASR chunks, not transcribe 1s clips"
    )


# --------------------------------------------------------------------------- #
# Utterance attribution
# --------------------------------------------------------------------------- #

def test_utterances_attributed_by_overlap():
    utterances = [
        {"start": 0.0,  "end": 10.0, "text": "first"},
        {"start": 10.0, "end": 20.0, "text": "second"},
    ]
    win = {"start_time": 8.0, "end_time": 9.0}
    assert [u["text"] for u in _utterances_for_window(win, utterances)] == ["first"]

    win = {"start_time": 9.5, "end_time": 10.5}   # straddles the boundary
    assert [u["text"] for u in _utterances_for_window(win, utterances)] == \
        ["first", "second"]

    win = {"start_time": 30.0, "end_time": 31.0}  # silence
    assert _utterances_for_window(win, utterances) == []


# --------------------------------------------------------------------------- #
# Document shape — must match the describe/semantic docs
# --------------------------------------------------------------------------- #

def test_document_structure_and_effective_params():
    utterances = [{"start": 0.0, "end": 10.0, "text": "hello"}]
    timeline = [{
        "start": 0.0, "end": 1.0,
        "transcript": utterances,
        "visual": "- a cat",
        "keyframes": [{"t": 0.5, "filename": "kf_000000500.jpg", "caption": "a cat"}],
    }]
    doc = _assemble_document(
        title="T", info=INFO, has_audio=True, window_secs=1, asr_chunk_secs=10.0,
        visual_fps=1.0, keyframes=True, language=None,
        timeline=timeline, utterances=utterances,
    )
    assert doc["version"] == 1
    assert doc["kind"] == "semantic_timeline"
    assert set(doc) == {"version", "kind", "video", "params", "timeline",
                        "transcript_text"}
    assert set(doc["video"]) == {"title", "duration_s", "fps", "has_audio"}
    assert doc["params"]["window_secs"] == 1
    assert doc["params"]["asr_chunk_secs"] == 10.0
    assert doc["params"]["windows"] == 1
    assert doc["video"]["duration_s"] == DURATION
    entry = doc["timeline"][0]
    assert set(entry) == {"start", "end", "transcript", "visual", "keyframes"}


def test_transcript_text_not_duplicated_across_fine_windows():
    """One utterance overlapping many windows must appear once in the flat text."""
    utterances = [{"start": 0.0, "end": 10.0, "text": "hello"}]
    windows = [{"start_time": float(i), "end_time": float(i + 1)} for i in range(10)]
    timeline = [
        {"start": w["start_time"], "end": w["end_time"],
         "transcript": _utterances_for_window(w, utterances),
         "visual": "", "keyframes": []}
        for w in windows
    ]
    assert all(len(e["transcript"]) == 1 for e in timeline)
    doc = _assemble_document(
        title=None, info=INFO, has_audio=True, window_secs=1, asr_chunk_secs=10.0,
        visual_fps=1.0, keyframes=False, language=None,
        timeline=timeline, utterances=utterances,
    )
    assert doc["transcript_text"] == "hello"


# --------------------------------------------------------------------------- #
# Keyframe budget
# --------------------------------------------------------------------------- #

def test_keyframe_budget_scales_with_window():
    d = _describer()
    samples_1s, max_1s = d._keyframe_budget(1.0)
    samples_60, max_60 = d._keyframe_budget(60.0)
    assert max_1s == 1 and max_60 == 2
    assert samples_1s < samples_60
    assert samples_60 == SemanticDescriber.KEYFRAME_SAMPLES


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def _request(**kw):
    return DescribeRequest(source={"url": "https://example.com/a.mp4"}, **kw)


def test_accepts_min_default_and_max():
    assert _request(window_secs=MIN_WINDOW_SECS).window_secs == MIN_WINDOW_SECS
    assert _request(window_secs=MAX_WINDOW_SECS).window_secs == MAX_WINDOW_SECS
    assert _request().window_secs == DEFAULT_WINDOW_SECS


def test_rejects_out_of_range_with_actionable_message():
    for bad in (0, -5, MAX_WINDOW_SECS + 1, 300):
        try:
            _request(window_secs=bad)
        except ValidationError as exc:
            msg = str(exc)
            assert "window_secs must be between" in msg, msg
            assert str(MAX_WINDOW_SECS) in msg
        else:
            raise AssertionError(f"window_secs={bad} was accepted")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
