#!/usr/bin/env python3
"""
VideoARM — command-line entry point.

Examples
--------
# Open-ended question (QA mode)
python main.py --video lecture.mp4 --question "What topics are covered?"

# Multiple-choice
python main.py --video video.mp4 --question "A. ... B. ... C. ... D. ..." --multiple-choice

# Dense lecture notes → LaTeX + PDF  (lecture mode)
python main.py --lecture --video lecture.mp4 --title "Linear Algebra" --course "MATH 301"
python main.py --lecture --video lecture.mp4 --title "Notes" --output ./my_notes
"""

import argparse
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VideoARM: Agentic Reasoning-over-Hierarchical-Memory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--video", "-v", required=True,
                        help="Path to local video file.")

    # ── QA mode ──────────────────────────────────────────────────────────
    parser.add_argument("--question", "-q", default=None,
                        help="Question to answer (QA mode).")
    parser.add_argument("--model", "-m", default=None,
                        help="Override controller model.")
    parser.add_argument("--multiple-choice", "--mc", action="store_true",
                        help="Multiple-choice mode — respond with a single letter or number.")
    parser.add_argument("--choice-format", choices=["letter", "number"],
                        default="letter",
                        help="Answer format: 'letter' (A-D) or 'number' (0-4).")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not save the QA trace to disk.")

    # ── Lecture mode ──────────────────────────────────────────────────────
    parser.add_argument("--lecture", "-l", action="store_true",
                        help="Lecture summarization mode: generate dense LaTeX notes + PDF.")
    parser.add_argument("--title", default="Lecture Notes",
                        help="Document title (lecture mode, default: 'Lecture Notes').")
    parser.add_argument("--course", default="",
                        help="Course name / subtitle (lecture mode).")
    parser.add_argument("--output", "-o", default="output",
                        help="Output directory for .tex and .pdf (lecture mode, default: output/).")
    parser.add_argument("--stem", default="lecture_notes",
                        help="Base filename without extension (default: lecture_notes).")
    parser.add_argument("--output-language", "--out-lang", default="en",
                        help="Language the finished notes are written in "
                             "(EN, AR, HE, …; default: EN). Unknown codes fall back to English.")

    # ── Shared ────────────────────────────────────────────────────────────
    parser.add_argument("--verbose", "-V", action="store_true",
                        help="Show detailed execution output.")

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    try:
        if args.lecture:
            _run_lecture_mode(args)
        else:
            _run_qa_mode(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Lecture mode
# ---------------------------------------------------------------------------

def _run_lecture_mode(args: argparse.Namespace) -> None:
    from videoarm.core.lecture_summarizer import LectureSummarizer

    summarizer = LectureSummarizer(model_name=args.model)
    pdf_path = summarizer.summarize(
        video_path=args.video,
        title=args.title,
        course=args.course,
        output_dir=args.output,
        stem=args.stem,
        output_language=args.output_language,
    )
    print(f"\nLecture notes PDF: {pdf_path}")


# ---------------------------------------------------------------------------
# QA mode
# ---------------------------------------------------------------------------

def _run_qa_mode(args: argparse.Namespace) -> None:
    if not args.question:
        print("Error: --question is required in QA mode (or use --lecture).",
              file=sys.stderr)
        sys.exit(1)

    from videoarm.core.agent import VideoARMAgent

    agent = VideoARMAgent(model_name=args.model)
    answer = agent.ask(
        args.video,
        args.question,
        is_multiple_choice=args.multiple_choice,
        choice_format=args.choice_format,
        save_result=not args.no_save,
    )
    print(f"\nAnswer: {answer}")


if __name__ == "__main__":
    main()
