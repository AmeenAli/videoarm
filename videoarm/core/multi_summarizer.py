"""
MultiVideoSummarizer — ONE combined LaTeX document / PDF from a stack of videos.

This is the dedicated service behind POST /v1/summarize/multi. It is deliberately
a thin layer over the single-video pipeline so the regular lecture route is
untouched: each video runs through the standard ``LectureSummarizer`` pipeline
(``summarize_body`` — ASR → visual extraction → figures → LaTeX synthesis), and
the per-video bodies are stitched into a single document in submission order,
one ``\\section`` per video, then compiled once.

Videos are processed sequentially — segment/visual concurrency inside each video
already saturates the shared GPU, so interleaving whole videos would add memory
pressure without throughput.
"""

import re
import time
from typing import Any, Callable, Dict, List, Optional

import cv2

from videoarm.core.lecture_summarizer import LectureSummarizer
from videoarm.latex.renderer import _escape_text, build_and_compile


def probe_segment_count(video_path: str) -> int:
    """Number of segments LectureSummarizer will plan for this video.

    Mirrors ``LectureSummarizer._plan_segments`` (same cv2 metadata, same
    5-min/30-s-overlap arithmetic) so the aggregate progress total reported
    before processing starts matches the per-video totals exactly. Raises if
    the file is unreadable — better to fail the job up front than mid-stack.
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if fps <= 0 or total_frames <= 0:
        raise RuntimeError(f"Cannot read video metadata (corrupt or unsupported file): {video_path}")

    seg_frames     = int(LectureSummarizer.SEGMENT_SECS * fps)
    overlap_frames = int(LectureSummarizer.OVERLAP_SECS * fps)
    count, start = 0, 0
    while start < total_frames:
        end = min(start + seg_frames - 1, total_frames - 1)
        count += 1
        if end >= total_frames - 1:
            break
        start = end - overlap_frames + 1
    return count


class MultiVideoSummarizer:
    """
    Combine several videos into one set of notes.

    Usage::

        s = MultiVideoSummarizer()
        pdf = s.summarize(
            videos=[{"path": "part1.mp4", "title": "Part 1"},
                    {"path": "part2.mp4", "title": "Part 2"}],
            title="Linear Algebra — Full Course",
            output_dir="output", stem="course_notes",
        )
    """

    def summarize(
        self,
        videos: List[Dict[str, Any]],
        title: str = "Combined Notes",
        course: str = "",
        output_dir: str = "output",
        stem: str = "notes",
        language: Optional[str] = None,
        domain: str = "",
        intent: str = "",
        on_progress: Optional[Callable[[int, int], None]] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        output_language: str = "en",
    ) -> str:
        """
        Process every video and return the path to the single combined PDF.

        Args:
            videos:  Ordered list of ``{"path": <local file>, "title": <optional>}``.
                     Order is preserved — video i becomes \\section i of the notes.
            (remaining args identical to LectureSummarizer.summarize and applied
            to every video; `language`/`output_language` are job-wide.)

        Progress: ``on_progress(done, total)`` counts SEGMENTS aggregated across
        all videos, so the API's estimated_seconds_remaining stays meaningful.
        """
        if not videos:
            raise ValueError("videos list is empty")

        # Probe all files up front: exact grand total for progress, and any
        # unreadable file fails the job before GPU time is spent.
        seg_counts  = [probe_segment_count(str(v["path"])) for v in videos]
        grand_total = sum(seg_counts)
        if on_progress:
            on_progress(0, grand_total)

        print("=" * 60)
        print("VideoARM — Multi-Video Summarizer")
        print(f"  Videos   : {len(videos)}  |  total segments: {grand_total}")
        print(f"  Title    : {title}")
        print("=" * 60)

        start_wall = time.time()
        bodies: List[str] = []
        done_offset = 0

        for i, (video, n_segs) in enumerate(zip(videos, seg_counts), 1):
            part_title = (video.get("title") or "").strip() or f"Video {i}"
            print(f"\n■ Video {i}/{len(videos)} — {part_title} "
                  f"({n_segs} segments)\n")

            def _offset_progress(done: int, total: int,  # noqa: ARG001
                                 _offset: int = done_offset) -> None:
                if on_progress:
                    on_progress(_offset + done, grand_total)

            # Fresh summarizer per video: the underlying agent keeps per-video
            # state (video_info, session frames) that must not leak across files.
            summarizer = LectureSummarizer()
            try:
                body = summarizer.summarize_body(
                    video_path=str(video["path"]),
                    title=part_title,
                    output_dir=output_dir,
                    language=language,
                    domain=domain,
                    intent=intent,
                    on_progress=_offset_progress,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_language=output_language,
                    figure_prefix=f"v{i:02d}_",
                )
            except Exception as exc:
                raise RuntimeError(f"Video {i} ('{part_title}') failed: {exc}") from exc

            # The synthesis prompt asks for \subsection-level output, but the
            # model occasionally emits a stray \section; demote those so each
            # video remains exactly one \section of the combined document.
            body = re.sub(r"\\section(\*?)\s*\{", r"\\subsection\1{", body)
            bodies.append(f"\\section{{{_escape_text(part_title)}}}\n\n{body}")
            done_offset += n_segs
            if on_progress:
                on_progress(done_offset, grand_total)

        # \clearpage between videos flushes each part's floats (figures) before
        # the next section starts.
        combined = "\n\n\\clearpage\n\n".join(bodies)
        print(f"\nAll {len(videos)} videos processed in "
              f"{time.time() - start_wall:.0f}s — compiling combined PDF …\n")

        pdf_path = build_and_compile(
            body=combined, title=title, course=course,
            output_dir=output_dir, stem=stem,
            output_language=output_language,
        )
        print(f"Combined PDF: {pdf_path}")
        return pdf_path
