#!/usr/bin/env python3
"""
End-to-end test: dense LaTeX lecture notes from a video → PDF.

Usage:
    python test_lecture.py [video] [language]

    video     path to video file   (default: main.mp4)
    language  ISO 639-1 code       (default: auto-detect)

Examples:
    python test_lecture.py                      # main.mp4, auto language
    python test_lecture.py vid2.mp4 he          # Hebrew lecture
"""

import sys
import time
from pathlib import Path

VIDEO      = sys.argv[1] if len(sys.argv) > 1 else "main.mp4"
LANGUAGE   = sys.argv[2] if len(sys.argv) > 2 else None
OUTPUT_DIR = "output"

# ── pre-flight checks ─────────────────────────────────────────────────────────
if not Path(VIDEO).exists():
    print(f"✗  {VIDEO} not found in {Path.cwd()}", file=sys.stderr)
    sys.exit(1)

import requests

def _check_server(url: str, name: str) -> bool:
    try:
        r = requests.get(f"{url}/health", timeout=5)
        if r.status_code == 200:
            print(f"  ✓  {name} ({url}) — healthy")
            return True
    except Exception:
        pass
    print(f"  ✗  {name} ({url}) — NOT reachable  (run: bash start_servers.sh)")
    return False

print("━" * 64)
print("  VideoARM — Lecture Note Test")
print("━" * 64)
print("\nServer health checks:")
llm_ok = _check_server("http://localhost:8000", "LLM  (port 8000)")
asr_ok = _check_server("http://localhost:8001", "ASR  (port 8001)")

if not (llm_ok and asr_ok):
    print("\nPlease start the servers first:  bash start_servers.sh")
    sys.exit(1)

# ── load dotenv ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── run ───────────────────────────────────────────────────────────────────────
from videoarm.core.lecture_summarizer import LectureSummarizer

t_total_start = time.perf_counter()

stem = Path(VIDEO).stem  # e.g. "vid2" for vid2.mp4

print(f"\nVideo    : {VIDEO}  ({Path(VIDEO).stat().st_size / 1e6:.1f} MB)")
print(f"Language : {LANGUAGE or 'auto-detect'}")
print(f"Output   : {OUTPUT_DIR}/{stem}.pdf\n")

summarizer = LectureSummarizer()

t_proc_start = time.perf_counter()
pdf_path = summarizer.summarize(
    video_path=VIDEO,
    title="Lecture Notes",
    course="",
    output_dir=OUTPUT_DIR,
    stem=stem,
    language=LANGUAGE,
)
t_proc_end = time.perf_counter()

total_elapsed = t_proc_end - t_total_start
proc_elapsed  = t_proc_end - t_proc_start

# ── report ────────────────────────────────────────────────────────────────────
print("\n" + "━" * 64)
print("  TIMING REPORT")
print("━" * 64)

video_duration = summarizer.agent.video_info.get("duration", 0)
fps            = summarizer.agent.video_info.get("fps", 0)
total_frames   = summarizer.agent.video_info.get("total_frames", 0)

print(f"  Video duration   : {video_duration:.1f}s  ({video_duration/60:.1f} min)")
print(f"  Total frames     : {total_frames}  @  {fps:.1f} fps")
print(f"  Processing time  : {proc_elapsed:.1f}s  ({proc_elapsed/60:.1f} min)")
if video_duration > 0:
    print(f"  Speed ratio      : {video_duration / proc_elapsed:.2f}× real-time")
print(f"  Total wall time  : {total_elapsed:.1f}s")
print(f"  PDF saved to     : {pdf_path}")
print(f"  Language         : {LANGUAGE or 'auto-detect'}")
print("━" * 64)
