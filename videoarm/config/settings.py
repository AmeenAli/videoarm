"""
VideoARM path configuration.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent

# Temporary frames written during inference (auto-cleaned per session)
TEMP_DIR = PROJECT_ROOT / "tmp"

# QA result JSON files (only written when save_result=True)
RESULTS_DIR = PROJECT_ROOT / "results"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")

# ---------------------------------------------------------------------------
# Semantic timeline (POST /v1/describe) — resolution
# ---------------------------------------------------------------------------
# Timeline resolution: seconds of video per timeline window. Each window gets
# its own visual observation, keyframes and transcript, so this is the knob
# behind the app's resolution slider. Product range is 1s (finest) to 120s.
MIN_WINDOW_SECS = 1
MAX_WINDOW_SECS = 120
DEFAULT_WINDOW_SECS = 60

# Shortest audio slice worth sending to the ASR. The local model (Qwen3-ASR)
# returns no per-word timestamps, so a call's text can only be stamped with the
# span of audio it was given, and slices much shorter than this cut words
# mid-syllable and produce filler tokens. Timelines finer than this therefore
# share coarser ASR chunks: windows still advance at window_secs, while each
# transcript utterance carries its own (coarser) true span.
MIN_ASR_CHUNK_SECS = int(os.getenv("VIDEOARM_MIN_ASR_CHUNK_SECS", "10"))

# Timeline windows described in parallel. Fine resolutions produce many small
# model calls, so raising this improves GPU utilisation on a quiet box.
DESCRIBE_CONCURRENCY = int(os.getenv("VIDEOARM_DESCRIBE_CONCURRENCY", "3"))
