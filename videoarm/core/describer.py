"""
SemanticDescriber — raw, timestamped perception of a video, no reasoning.

Produces a machine-consumable JSON timeline of WHAT IS SEEN AND HEARD:
per-window audio transcript, objective visual observations, and optional
keyframe screenshots — with start/end timestamps on every element. There is
deliberately NO synthesis / analysis / summarisation stage: the output is
meant to be fed to any downstream agent, which does its own reasoning.

Reuses the same perception primitives as the lecture pipeline (VideoARMAgent
frame extraction + grids, the local ASR server, the vision model) but stops
before the "understanding" layer.
"""

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from videoarm.api.client import call_openai_model_with_tools
from videoarm.config.settings import (
    DEFAULT_WINDOW_SECS,
    DESCRIBE_CONCURRENCY,
    MAX_WINDOW_SECS,
    MIN_ASR_CHUNK_SECS,
    MIN_WINDOW_SECS,
)

# ---------------------------------------------------------------------------
# Prompts — observation only, explicitly no interpretation
# ---------------------------------------------------------------------------

_OBSERVE_SYSTEM = """\
You are a neutral visual observer converting video frames into a factual \
record of what is on screen. The frames are presented in temporal order; where \
several frames are tiled into one grid image, read it left-to-right, \
top-to-bottom.

Report ONLY what is visibly present:
  • People: count, appearance, visible actions and gestures
  • Objects, animals, vehicles, products — and what happens to them
  • Setting / location and any scene changes or camera cuts
  • On-screen text, captions, signs, UI elements — transcribe them verbatim
  • Graphics, diagrams, charts (describe what they depict, not what they imply)

Rules:
  • NO interpretation, NO analysis, NO guesses about intent, emotion, or story.
  • NO quality judgements. NO advice. Observation only.
  • Concise "- " bullet lines, present tense. Note visible changes over time
    ("a second person enters", "cut to an outdoor scene").
  • If frames are near-identical, describe the scene once and say it is static.\
"""

_OBSERVE_USER = """\
These frames cover {start_time:.1f}s–{end_time:.1f}s of a video, sampled at \
{fps:.1f} fps. List what is visible.\
"""

_KEYFRAME_SYSTEM = """\
You are selecting representative frames from a video window to serve as \
keyframe screenshots in a machine-readable index.

SELECT up to {max_k} frames that together best represent what is visible in \
this window (distinct scenes, subjects, or on-screen content). Prefer sharp, \
information-dense frames; skip blurry, transitional, or near-duplicate ones.

Respond with ONLY a valid JSON array — no prose, no markdown fences.
Each element: {{"frame_index": <int>, "caption": "<objective one-line description>"}}
If nothing is usable, return [].\
"""

_KEYFRAME_USER = """\
Frames {start_time:.0f}s–{end_time:.0f}s ({n_frames} frames, indices 0–{last_idx}).
Pick the representative frames. Return JSON only.\
"""


def _utterances_for_window(
    win: Dict[str, Any], utterances: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Utterances overlapping a window, keeping their own (true) spans.

    When the timeline is finer than the ASR grid one utterance legitimately
    appears in several consecutive windows; its `start`/`end` say so, and
    `transcript_text` is built from the utterance list rather than from the
    windows, so nothing is double-counted there.
    """
    return [
        u for u in utterances
        if u["end"] > win["start_time"] and u["start"] < win["end_time"]
    ]


def _assemble_document(
    *,
    title: Optional[str],
    info: Dict[str, Any],
    has_audio: bool,
    window_secs: int,
    asr_chunk_secs: float,
    visual_fps: float,
    keyframes: bool,
    language: Optional[str],
    timeline: List[Dict[str, Any]],
    utterances: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the semantic_timeline document (pure — no I/O, no model calls)."""
    return {
        "version": 1,
        "kind": "semantic_timeline",
        "video": {
            "title": title,
            "duration_s": round(info["total_frames"] / info["fps"], 2),
            "fps": info["fps"],
            "has_audio": has_audio,
        },
        "params": {
            "window_secs": window_secs,
            "asr_chunk_secs": asr_chunk_secs,
            "windows": len(timeline),
            "visual_fps": visual_fps,
            "keyframes": keyframes,
            "language": language,
        },
        "timeline": timeline,
        "transcript_text": " ".join(
            u["text"] for u in utterances if u["text"]
        ).strip(),
    }


class SemanticDescriber:
    """Convert a video into a timestamped JSON perception timeline."""

    VISUAL_FPS: float      = 1.0   # frames sampled per second for observation
    GRID_ROWS: int         = 2     # composite grid layout (matches lecture pipeline)
    GRID_COLS: int         = 3
    VISION_BATCH: int      = 5     # grid images per vision call
    KEYFRAME_SAMPLES: int  = 8     # 480px frames sampled per window for selection
    KEYFRAME_MAX: int      = 2     # max keyframes kept per window (long windows)
    WINDOW_CONCURRENCY: int = DESCRIBE_CONCURRENCY  # windows processed in parallel

    def __init__(self, model_name: Optional[str] = None) -> None:
        from videoarm.core.agent import VideoARMAgent  # noqa: PLC0415
        self.agent  = VideoARMAgent(model_name=model_name)
        self.config = self.agent.config

    # ------------------------------------------------------------------ #
    # Public                                                              #
    # ------------------------------------------------------------------ #

    def describe(
        self,
        video_path: str,
        output_dir: str = "output",
        title: Optional[str] = None,
        language: Optional[str] = None,
        window_secs: int = DEFAULT_WINDOW_SECS,
        keyframes: bool = True,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Run perception over the whole video and return the JSON document.

        `window_secs` is the timeline resolution (1–120s): each window gets its
        own visual observation, keyframes and transcript, stamped with the
        window's start/end in seconds. Cost scales inversely — halving the
        window roughly doubles the number of model calls.

        Audio is transcribed on a coarser grid than the timeline when the
        windows are short (see MIN_ASR_CHUNK_SECS): the local ASR emits no
        per-word timestamps, so an utterance can only be stamped with the span
        of audio the model was given, and one-second slices cut words in half.
        Each window therefore lists the utterances that overlap it, carrying
        their own — possibly wider — spans. `params.asr_chunk_secs` reports the
        transcript granularity actually used.
        """
        window_secs = max(MIN_WINDOW_SECS, min(MAX_WINDOW_SECS, int(window_secs)))

        start_wall = time.time()
        self.agent._initialize_video(video_path)
        self.agent.session_id      = str(int(time.time() * 1000))[-8:]
        self.agent.hm3             = self.agent._empty_hm3()
        self.agent.video_has_audio = True
        info = self.agent.video_info

        figures_dir = Path(output_dir) / "figures"
        if keyframes:
            figures_dir.mkdir(parents=True, exist_ok=True)

        windows        = self._plan_windows(info, window_secs)
        asr_chunks     = self._plan_asr_chunks(windows, window_secs)
        asr_chunk_secs = (round(asr_chunks[0]["end_time"] - asr_chunks[0]["start_time"], 2)
                          if asr_chunks else window_secs)
        total = len(windows)

        print("=" * 60)
        print("VideoARM — Semantic Describer")
        print(f"  Video   : {video_path}")
        print(f"  Window  : {window_secs}s  |  keyframes: {keyframes}")
        print(f"  Timeline: {total} windows  |  ASR chunks: {len(asr_chunks)} "
              f"@ {asr_chunk_secs:.0f}s")
        print("=" * 60)
        if total > 1000:
            print(f"  ⚠  {total} windows at {window_secs}s resolution — expect "
                  f"~{total * (2 if keyframes else 1) + len(asr_chunks)} model "
                  f"calls. Coarser window_secs is much cheaper.")

        if on_progress:
            on_progress(0, total)

        # Phase 1 — transcription on the (possibly coarser) ASR grid.
        utterances = self._transcribe_all(video_path, asr_chunks, info, language)

        # Phase 2 — per-window visual observation + keyframes.
        entries: List[Optional[Dict[str, Any]]] = [None] * total
        done = 0
        workers = max(1, min(self.WINDOW_CONCURRENCY, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._process_window, video_path, w, utterances,
                            keyframes, figures_dir, i + 1, total): i
                for i, w in enumerate(windows)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                entries[i] = fut.result()
                done += 1
                if on_progress:
                    on_progress(done, total)

        doc = _assemble_document(
            title=title,
            info=info,
            has_audio=self.agent.video_has_audio,
            window_secs=window_secs,
            asr_chunk_secs=asr_chunk_secs,
            visual_fps=self.VISUAL_FPS,
            keyframes=keyframes,
            language=language,
            timeline=[e for e in entries if e is not None],
            utterances=utterances,
        )

        self.agent._cleanup_temp_frames()
        print(f"Semantic timeline built in {time.time() - start_wall:.0f}s "
              f"({total} windows)")
        return doc

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _plan_windows(self, info: Dict[str, Any], window_secs: int) -> List[Dict[str, Any]]:
        """Cut the video into contiguous windows of `window_secs`.

        Windows tile the timeline without gaps: each one's `end_time` is the
        next one's `start_time`. A trailing remainder shorter than a quarter of
        a window is folded into the previous window rather than described on
        its own — a 0.2s window costs the same model calls as a full one.
        """
        fps          = info["fps"]
        total_frames = info["total_frames"]
        duration     = total_frames / fps
        win_frames   = max(1, int(round(window_secs * fps)))
        windows: List[Dict[str, Any]] = []
        start = 0
        while start < total_frames:
            end = min(start + win_frames - 1, total_frames - 1)
            windows.append({
                "start_frame": start,
                "end_frame":   end,
                "start_time":  start / fps,
                "end_time":    min((end + 1) / fps, duration),
                "duration":    (end - start + 1) / fps,
            })
            if end >= total_frames - 1:
                break
            start = end + 1

        if len(windows) > 1 and windows[-1]["duration"] < 0.25 * window_secs:
            tail = windows.pop()
            prev = windows[-1]
            prev["end_frame"] = tail["end_frame"]
            prev["end_time"]  = tail["end_time"]
            prev["duration"]  = (prev["end_frame"] - prev["start_frame"] + 1) / fps
        return windows

    def _plan_asr_chunks(
        self, windows: List[Dict[str, Any]], window_secs: int,
    ) -> List[Dict[str, Any]]:
        """Group consecutive windows into audio chunks of >= MIN_ASR_CHUNK_SECS.

        Chunks are a whole number of windows, so chunk boundaries always fall on
        window boundaries. At the default resolution one chunk == one window,
        which is the historical behaviour.
        """
        if not windows:
            return []
        per_chunk = max(1, math.ceil(MIN_ASR_CHUNK_SECS / max(1, window_secs)))
        chunks: List[Dict[str, Any]] = []
        for i in range(0, len(windows), per_chunk):
            group = windows[i:i + per_chunk]
            chunks.append({
                "start_frame": group[0]["start_frame"],
                "end_frame":   group[-1]["end_frame"],
                "start_time":  group[0]["start_time"],
                "end_time":    group[-1]["end_time"],
            })
        return chunks

    def _transcribe_all(
        self, video_path: str, chunks: List[Dict[str, Any]],
        info: Dict[str, Any], language: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Transcribe every ASR chunk (in parallel) → utterances in time order."""
        if not chunks:
            return []
        results: List[List[Dict[str, Any]]] = [[] for _ in chunks]
        workers = max(1, min(self.WINDOW_CONCURRENCY, len(chunks)))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._transcribe_chunk, video_path, c, info, language): i
                for i, c in enumerate(chunks)
            }
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
                done += 1
                if done % 10 == 0 or done == len(chunks):
                    print(f"│  Transcribed {done}/{len(chunks)} audio chunks")
        return [u for group in results for u in group]

    def _process_window(
        self,
        video_path: str,
        win: Dict[str, Any],
        utterances: List[Dict[str, Any]],
        keyframes: bool,
        figures_dir: Path,
        num: int,
        total: int,
    ) -> Dict[str, Any]:
        t0, t1 = win["start_time"], win["end_time"]
        visual = self._observe_window(video_path, win)
        kfs    = (self._select_keyframes(video_path, win, figures_dir)
                  if keyframes else [])
        print(f"└─ Window {num}/{total} ({t0:.1f}s–{t1:.1f}s) done "
              f"({len(visual)} chars visual, {len(kfs)} keyframes)")
        return {
            "start": round(t0, 2),
            "end":   round(t1, 2),
            "transcript": _utterances_for_window(win, utterances),
            "visual": visual,
            "keyframes": kfs,
        }

    def _transcribe_chunk(
        self, video_path: str, chunk: Dict[str, Any], info: Dict[str, Any],
        language: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Return utterances [{start, end, text}]. The ASR gives global frame
        indices per transcription call; when it can't (plain-text fallback),
        the utterance spans the whole chunk — timing is always present."""
        fps = info["fps"]
        try:
            result = self.agent._audio_transcriber(
                video_path=video_path,
                frame_ranges=[{"start_frame": chunk["start_frame"],
                               "end_frame":   chunk["end_frame"]}],
                reason="Semantic timeline transcription",
                language=language,
            )
        except Exception as exc:
            print(f"│  ⚠  ASR failed for chunk: {exc}")
            return []
        if result.get("status") == "no_audio" or result.get("error"):
            return []

        utterances: List[Dict[str, Any]] = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            s = seg.get("start_frame", 0) / fps
            e = seg.get("end_frame", 0) / fps
            if e <= s:  # plain-text fallback carries no real timing
                s, e = chunk["start_time"], chunk["end_time"]
            utterances.append({"start": round(s, 2), "end": round(e, 2),
                               "text": text})
        if not utterances:
            text = (result.get("transcript_text") or "").strip()
            if text:
                utterances = [{"start": round(chunk["start_time"], 2),
                               "end":   round(chunk["end_time"], 2),
                               "text":  text}]
        return utterances

    def _observe_window(self, video_path: str, win: Dict[str, Any]) -> str:
        n_frames = max(1, int(self.VISUAL_FPS * win["duration"]))
        sess = f"{self.agent.session_id}_d{win['start_frame']}"
        try:
            frame_paths = self.agent._extract_frames_proportional(
                video_path=video_path,
                frame_ranges=[{"start_frame": win["start_frame"],
                               "end_frame":   win["end_frame"]}],
                total_frames=n_frames,
                target_short_side=256,
                silent=True,
                session_id=sess,
            )
        except Exception as exc:
            print(f"│  ⚠  frame extraction failed: {exc}")
            return ""
        if not frame_paths:
            return ""

        # Grids pad any incomplete tile with black. At fine resolutions a window
        # holds fewer frames than one grid, so tiling would hand the model an
        # image that is mostly black padding — send the frames as-is instead.
        if len(frame_paths) < self.GRID_ROWS * self.GRID_COLS:
            images = list(frame_paths)
        else:
            composites = self.agent._make_composite_grids(
                frame_paths, rows=self.GRID_ROWS, cols=self.GRID_COLS)
            images = [str(p) for p in composites] if composites else frame_paths

        model = self.config.get_model("clip_analyzer")
        api_key, base_url = self.config.get_api_config("clip_analyzer")
        params = {
            **self.config.get_model_params("clip_analyzer"),
            "max_tokens": 600,
            "temperature": 0.0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        user_msg = _OBSERVE_USER.format(start_time=win["start_time"],
                                        end_time=win["end_time"],
                                        fps=self.VISUAL_FPS)
        parts: List[str] = []
        for batch in (images[j:j + self.VISION_BATCH]
                      for j in range(0, len(images), self.VISION_BATCH)):
            try:
                response = self.agent._run_with_retry(
                    lambda b=batch: call_openai_model_with_tools(
                        messages=[
                            {"role": "system", "content": _OBSERVE_SYSTEM},
                            {"role": "user",   "content": user_msg},
                        ],
                        model_name=model,
                        endpoints=base_url,
                        api_key=api_key,
                        image_paths=b,
                        **params,
                    )
                )
                content = (response or {}).get("content", "").strip()
                if content:
                    parts.append(content)
            except Exception as exc:
                print(f"│  ⚠  observation call failed: {exc}")
        return "\n".join(parts)

    def _keyframe_budget(self, duration: float) -> tuple:
        """(frames sampled, keyframes kept) for a window of `duration` seconds.

        Sampling eight 480px frames to choose from — and keeping two of them —
        only makes sense for a window with room for two distinct moments. Short
        windows get proportionally fewer samples and a single keyframe, which
        keeps a 1s timeline from emitting two near-identical screenshots per
        second of video.
        """
        max_k   = self.KEYFRAME_MAX if duration >= 10 else 1
        samples = min(self.KEYFRAME_SAMPLES, max(2, int(duration * 2)))
        return samples, max_k

    def _select_keyframes(
        self, video_path: str, win: Dict[str, Any], figures_dir: Path,
    ) -> List[Dict[str, Any]]:
        import re      # noqa: PLC0415
        import shutil  # noqa: PLC0415

        n_samples, max_k = self._keyframe_budget(win["duration"])
        try:
            frame_paths = self.agent._extract_frames_proportional(
                video_path=video_path,
                frame_ranges=[{"start_frame": win["start_frame"],
                               "end_frame":   win["end_frame"]}],
                total_frames=n_samples,
                target_short_side=480,
                silent=True,
                session_id=f"{self.agent.session_id}_k{win['start_frame']}",
            )
        except Exception:
            return []
        if not frame_paths:
            return []

        n = len(frame_paths)
        model = self.config.get_model("clip_analyzer")
        api_key, base_url = self.config.get_api_config("clip_analyzer")
        params = {
            **self.config.get_model_params("clip_analyzer"),
            "max_tokens": 300,
            "temperature": 0.0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        try:
            response = self.agent._run_with_retry(
                lambda: call_openai_model_with_tools(
                    messages=[
                        {"role": "system",
                         "content": _KEYFRAME_SYSTEM.format(max_k=max_k)},
                        {"role": "user",
                         "content": _KEYFRAME_USER.format(
                             start_time=win["start_time"],
                             end_time=win["end_time"],
                             n_frames=n, last_idx=n - 1)},
                    ],
                    model_name=model,
                    endpoints=base_url,
                    api_key=api_key,
                    image_paths=[str(p) for p in frame_paths],
                    **params,
                )
            )
            raw = (response or {}).get("content", "").strip()
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            selections = json.loads(m.group(0)) if m else []
        except Exception as exc:
            print(f"│  ⚠  keyframe selection failed: {exc}")
            return []

        results: List[Dict[str, Any]] = []
        for item in selections[:max_k]:
            idx = item.get("frame_index")
            caption = (item.get("caption") or "").strip()
            if not isinstance(idx, int) or not (0 <= idx < n):
                continue
            # Frames are sampled proportionally across the window: index i sits
            # at the centre of its 1/n slice of the window.
            t = win["start_time"] + (idx + 0.5) / n * win["duration"]
            src = Path(str(frame_paths[idx]))
            dest_name = f"kf_{int(t * 1000):09d}{src.suffix}"
            shutil.copy2(src, figures_dir / dest_name)
            results.append({"t": round(t, 2), "filename": dest_name,
                            "caption": caption})
        return results
