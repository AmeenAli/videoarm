"""
LectureSummarizer — dense LaTeX lecture notes from video.

Audience: university lectures.
Goal    : capture EVERY detail — math in LaTeX, section hierarchy, proofs,
          examples, derivations, code — as if a meticulous student wrote
          everything down.

Pipeline per 5-minute segment
------------------------------
1. Audio transcription  (local Qwen3-ASR-1.7B)
2. Visual extraction    — segment split into 1-minute sub-windows;
                          each sampled at 4 fps → 240 frames → 4×5 grids
                          → 12 grid images per vision call
3. LaTeX section draft  (Qwen/Qwen3.6-35B-A3B via vllm, synthesis prompt)

Final step
----------
Concatenate per-segment LaTeX sections → full document → xelatex → PDF.
"""

import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from videoarm.api.client import call_openai_model_with_tools
from videoarm.latex.languages import LangSpec, resolve
from videoarm.latex.renderer import build_and_compile


def _language_directive(spec: LangSpec) -> str:
    """Instruction telling the model which language to WRITE the notes in.

    Injected into the system prompt so the finished notes — headings, prose,
    definitions, captions, everything — come out in the caller's chosen output
    language, regardless of the language spoken in the video."""
    lang = spec.name
    directive = (
        f"OUTPUT LANGUAGE: Write ALL notes in {lang} — every section heading, "
        f"sentence, definition, theorem statement, remark, and figure caption. "
        f"The lecture audio and on-screen text may be in any language; translate "
        f"and render everything into {lang}. Do NOT translate mathematics or code: "
        f"LaTeX math is language-neutral, so keep variable names, symbols, and "
        f"listings exactly as they are. Keep widely-standard technical terms or "
        f"proper nouns in their conventional form, adding a gloss in {lang} where "
        f"it aids understanding."
    )
    if spec.rtl:
        directive += (
            f" The document is typeset right-to-left automatically — simply write "
            f"natural {lang} prose and add no direction/bidi commands; leave every "
            f"LaTeX math expression and inline code in ordinary left-to-right form "
            f"(the compiler handles bidirectionality)."
        )
    return directive


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_NOTE_TAKER = """\
You are the most meticulous academic note-taker who has ever attended a \
university lecture. Future students will study SOLELY from your notes — \
completeness, accuracy, and professional quality are paramount.

════════════════════════════════════════════════════
  AUDIO TRANSCRIPTION RULES
════════════════════════════════════════════════════
• Write out EVERYTHING the lecturer says — do not paraphrase, condense, or omit.
• Preserve the exact sequence of explanations, questions, and remarks.
• When the lecturer gives an intuition, analogy, or aside, include it word-for-word
  as a \\begin{remark} or \\begin{note} environment.
• Even casual asides ("by the way", "notice that", "this is important") belong in
  the notes — they reveal what the lecturer considers significant.

════════════════════════════════════════════════════
  PROFESSIONAL ACADEMIC TONE
════════════════════════════════════════════════════
• Write as a polished graduate-level textbook, not lecture notes verbatim.
• Every section opens with a brief motivating paragraph in formal prose.
• Definitions, theorems, and proofs are typeset with full formal precision.
• Connect ideas explicitly: "This result generalises…", "The key insight is…",
  "Combining with the previous lemma…".

════════════════════════════════════════════════════
  LATEX QUALITY RULES  (compiler must not see errors)
════════════════════════════════════════════════════
1. MATH — always use LaTeX, never plain text or Unicode:
   • Inline:   $x^2 + y^2 = r^2$
   • Display:  \\begin{equation} ... \\end{equation}
   • Aligned:  \\begin{align} ... \\end{align}
   • Banned:   α β γ ∑ ∫ ∞  → use \\alpha \\beta \\gamma \\sum \\int \\infty

2. ENVIRONMENTS — use ONLY these (they are defined in the preamble):
   \\begin{definition}[Title]  ...  \\end{definition}
   \\begin{theorem}[Name]      ...  \\end{theorem}
   \\begin{lemma}              ...  \\end{lemma}
   \\begin{corollary}          ...  \\end{corollary}
   \\begin{proposition}        ...  \\end{proposition}
   \\begin{proof}              ...  \\end{proof}
   \\begin{example}            ...  \\end{example}
   \\begin{remark}             ...  \\end{remark}
   \\begin{note}               ...  \\end{note}
   \\begin{observation}        ...  \\end{observation}
   \\begin{claim}              ...  \\end{claim}
   \\begin{assumption}         ...  \\end{assumption}
   \\begin{conclusion}         ...  \\end{conclusion}
   DO NOT invent environments (no \\begin{diagram}, \\begin{transcription}, etc.).

3. STRUCTURE:
   \\section{} > \\subsection{} > \\subsubsection{} > \\paragraph{}
   Every section must contain prose — no floating bare environments.

4. FLOW — write continuous, readable prose:
   • Open each subsection with 1–2 sentences of context before the first env.
   • After every theorem/definition, add a sentence connecting it to what follows.
   • Derivations: introduce each step in prose, then show the math.
   • Never drop from one \\begin{} straight into another without a bridge.

5. SPECIAL CHARACTERS — escape in text mode:
   & → \\&    % → \\%    # → \\#    _ → \\_ (outside math)
   $ → \\$    { → \\{    } → \\}    ~ → \\textasciitilde{}

6. COMPLETENESS:
   • Reproduce ALL derivation steps — never write "it can be shown that".
   • Include every intuition, analogy, motivation, and aside the lecturer gave.
   • Code → \\begin{lstlisting}[language=Python] ... \\end{lstlisting}

7. OUTPUT: ONLY LaTeX body — no \\documentclass, no \\begin{document}.\
"""

_VISUAL_EXTRACTION_SYSTEM = """\
You are transcribing EVERY piece of information visible in these university \
lecture frames. Your output feeds a note-taking pipeline — be exhaustive.
If text is in a non-Latin script (Hebrew, Arabic …), transcribe it verbatim \
then add an English translation in parentheses.\
"""

_VISUAL_EXTRACTION_USER = """\
These frames cover {start_time:.1f}s – {end_time:.1f}s of a university lecture \
(sub-window {sub_idx}/{total_sub}, batch of frames sampled at 4 fps).

Give CONCISE but COMPLETE bullet-point notes. Be dense — one bullet per distinct element.

1. Slide/board title (if visible).
2. All math — LaTeX only: $inline$ or \\begin{{equation}}...\\end{{equation}}. No Unicode.
3. Key definitions, theorems, or steps written on screen — verbatim.
4. Diagrams: list axes, labels, curves, and any annotations in one line each.
5. Bullet hierarchies and numbered lists — preserve structure.
6. Any code — exact, with language noted.
7. Anything boxed, circled, underlined, or highlighted.

Non-Latin text (Hebrew, Arabic…): give original then English in parentheses.
Skip decorative or unchanged background elements.
Respond in dense bullet points, NOT prose paragraphs.\
"""

_POSTPROCESS_SYSTEM = """\
You are an expert LaTeX editor. You will receive one section of academic \
lecture notes written in LaTeX. Fix every error you find — do NOT add new \
content or remove existing content.

WHAT TO FIX:
1. Typos in prose and math command names (e.g. \\apha → \\alpha, \\lmabda → \\lambda)
2. Unclosed braces, mismatched \\begin/\\end pairs, invented environment names
   → replace any non-standard environment with the nearest valid one from the list:
   definition, theorem, lemma, corollary, proposition, proof, example,
   remark, note, observation, claim, assumption, conclusion
3. Unicode math symbols in text or math mode → replace with proper LaTeX macros
4. Unescaped special characters in text mode: & % # _ $ { } → escape them
5. Two consecutive \\begin{...} blocks with no prose in between →
   insert exactly ONE short bridging sentence between them
6. Sections or subsections with no prose body → add a one-sentence intro
7. Broken display math: fix \\begin{equation}/\\end{equation} mismatches,
   ensure \\[ ... \\] are closed, fix align/align* mismatches

HARD CONSTRAINTS:
• Output ONLY the corrected LaTeX body — no preamble, no \\documentclass,
  no \\begin{document}, no explanation text before or after
• Preserve every theorem, definition, proof, remark, and example — do not delete
• Preserve all section/subsection hierarchy exactly as given\
"""

_POSTPROCESS_USER = """\
Proofread and fix the LaTeX below. Return ONLY the corrected LaTeX body.

{latex}\
"""

_FIGURE_SELECTION_SYSTEM = """\
You are reviewing individual frames from a university lecture to decide which \
are worth including verbatim as figures in printed lecture notes.

SELECT a frame only if it contains:
  • Diagrams (block diagrams, commutative diagrams, flowcharts, automata)
  • Plots or graphs (function curves, scatter plots, histograms, loss curves)
  • Geometric constructions or illustrations
  • Circuit or system diagrams
  • Algorithm visualisations or decision trees
  • Any visual whose information cannot be reproduced in text or LaTeX math

DO NOT SELECT:
  • Text-only slides (already captured by the transcript)
  • Blurry, duplicate, or transitional frames
  • Frames showing only the lecturer / audience

Respond with ONLY a valid JSON array — no prose, no markdown fences.
Each element: {"frame_index": <int>, "caption": "<concise descriptive caption>"}
Maximum 3 elements. If nothing qualifies, return [].
Example: [{"frame_index": 4, "caption": "Block diagram of the encoder-decoder architecture"}]\
"""

_FIGURE_SELECTION_USER = """\
Frames {start_time:.0f}s–{end_time:.0f}s ({n_frames} frames, indices 0–{last_idx}).
Which frames contain diagrams, plots, or figures worth including in lecture notes?
Return JSON only.\
"""

# Custom-prompt jobs are not lectures: the lecture selector above rejects
# everything that isn't a diagram/plot (including every frame of e.g. a film
# being analysed), which left custom jobs with zero screenshots. Judge frames
# against the caller's actual task instead.
_FIGURE_SELECTION_SYSTEM_CUSTOM = """\
You are reviewing individual frames from a video to decide which are worth \
including verbatim as figures (screenshots) in a written document about that \
video. Judge each frame by how much it would add to THE DOCUMENT'S TASK below \
— not by lecture-notes criteria.

═══ THE DOCUMENT'S TASK ═══
{task_context}

SELECT a frame if it is visually informative for that task, for example:
  • Key shots, compositions, or scene moments the document would discuss
  • Diagrams, charts, on-screen text or graphics, UI screens, product views
  • Any visual whose content the document would reference or analyse

DO NOT SELECT:
  • Blurry, duplicate, transitional, or near-identical frames
  • Frames that add nothing over ones already selected

Respond with ONLY a valid JSON array — no prose, no markdown fences.
Each element: {{"frame_index": <int>, "caption": "<concise descriptive caption>"}}
Maximum 3 elements. If nothing qualifies, return [].\
"""

_FIGURE_SELECTION_USER_CUSTOM = """\
Frames {start_time:.0f}s–{end_time:.0f}s ({n_frames} frames, indices 0–{last_idx}).
Which frames are worth including as figures in the document? Return JSON only.\
"""

_SEGMENT_SYNTHESIS_USER = """\
Write comprehensive, well-structured LaTeX lecture notes for the following \
{duration:.1f}-minute segment (segment {seg_num}/{total_segs}, \
{start_time:.1f}s–{end_time:.1f}s).

═══ AUDIO TRANSCRIPT ═══
{transcript}

═══ VISUAL CONTENT (4 fps, {n_subsegs} sub-windows) ═══
{visual_notes}

═══ FIGURES (screenshots extracted from this segment) ═══
{figures_block}

═══ TASK ═══
Produce dense, exhaustive, professionally written LaTeX body content.

AUDIO — this is your primary source:
• Write out EVERYTHING the lecturer said — full sentences, not bullet summaries.
• Every explanation, question posed, answer given, intuition offered, or casual
  remark must appear in the notes. Do not omit anything.
• When the lecturer explains a concept conversationally, render it as polished
  academic prose while preserving every idea.
• Verbatim quotes of important statements go in \\begin{{remark}} environments.

STRUCTURE
• Open with \\subsection{{...}} headings matching topics covered.
• Inside each subsection write 1–2 sentences of motivating context.
• Use \\subsubsection{{}} to separate distinct sub-topics within a subsection.

CONTENT — reproduce everything from both audio and visual:
• Every definition, theorem, lemma, proof, worked example.
• Every derivation step in full — never skip steps or say "similarly".
• Every remark, intuition, analogy, or motivational aside.
• If the lecturer wrote notation on the board, define it formally.

FLOW
• Connect environments with bridging prose:
  "This motivates the following definition…"
  "The proof relies on the lemma above…"
  "As an immediate consequence we obtain…"
  "Returning to the lecturer's explanation…"
• Never place two \\begin{{...}} blocks back-to-back without prose.

FIGURES
• For each figure listed above, place it immediately after the paragraph \
that first references or describes what it shows, using:
  \\begin{{figure}}[H]
  \\centering
  \\includegraphics[width=0.85\\linewidth]{{<path>}}
  \\caption{{<caption>}}
  \\end{{figure}}
  where <path> and <caption> come exactly from the FIGURES section above.
• If no figures are listed, do not write any \\includegraphics commands.

PROFESSIONAL TONE
• Write like a graduate-level textbook authored by the lecturer.
• Formal, precise, and self-contained. Every concept is fully explained.

QUALITY
• Use ONLY the environments listed in the system prompt — no invented ones.
• All math strictly in LaTeX — no Unicode math symbols.
• Escape special characters in text mode (& % # _ $ {{ }}).
• The output must compile with XeLaTeX without errors.

Output ONLY LaTeX body. No preamble. No \\begin{{document}}.\
"""

# Appended after a caller-supplied custom user prompt so the model still receives
# the per-segment transcript / visual notes / figures it must work from. The custom
# system + user prompts drive the style and task; this block only carries the data.
_CUSTOM_DATA_BLOCK = """\

═══ SEGMENT {seg_num}/{total_segs} ({start_time:.1f}s–{end_time:.1f}s, {duration:.1f} min) ═══

═══ AUDIO TRANSCRIPT ═══
{transcript}

═══ VISUAL CONTENT (sampled frames, {n_subsegs} sub-windows) ═══
{visual_notes}

═══ FIGURES (screenshots extracted from this segment) ═══
{figures_block}

Output ONLY a LaTeX body fragment (no \\documentclass, no \\begin{{document}}); it is \
concatenated with the other segments and compiled with XeLaTeX.\
"""


# ---------------------------------------------------------------------------
# LectureSummarizer
# ---------------------------------------------------------------------------

class LectureSummarizer:
    """
    Converts a university lecture video into a dense LaTeX document / PDF.

    Usage::

        s = LectureSummarizer()
        pdf = s.summarize("lecture.mp4", title="Linear Algebra", course="MATH 301")
    """

    SEGMENT_SECS: int   = 300   # 5-minute audio/synthesis segments
    OVERLAP_SECS: int   = 30    # 30-second look-ahead overlap between segments

    VISUAL_FPS: float   = 1.0   # frames sampled per second (1fps: 4× fewer calls, ~2-3hr for 111min video)
    VISUAL_SUBSEG_SECS: int = 60  # 1-minute sub-windows for visual extraction
    VISUAL_GRID_ROWS: int   = 2   # 2×3 = 6 frames per composite image
    VISUAL_GRID_COLS: int   = 3
    VISUAL_BATCH_SIZE: int  = 5   # grid images per vision API call (avoids vllm encoder cache race with TP=2)

    FIGURE_SAMPLES: int     = 15  # frames to sample per segment for figure selection
    FIGURE_MAX: int         = 3   # max figures to include per segment

    # Concurrency. The vLLM backend does continuous batching, so issuing the
    # independent model calls in parallel keeps the GPU busy and cuts wall-clock
    # time with no quality change. Peak concurrent requests ≈ the product of the
    # two; defaults stay well within vLLM's max-num-seqs on a single A100.
    SEGMENT_CONCURRENCY: int = int(os.getenv("VIDEOARM_SEGMENT_CONCURRENCY", "2"))
    VISUAL_CONCURRENCY: int  = int(os.getenv("VIDEOARM_VISUAL_CONCURRENCY", "4"))

    def __init__(self, model_name: Optional[str] = None) -> None:
        from videoarm.core.agent import VideoARMAgent
        self.agent  = VideoARMAgent(model_name=model_name)
        self.config = self.agent.config

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def summarize(
        self,
        video_path: str,
        title: str = "Lecture Notes",
        course: str = "",
        output_dir: str = "output",
        stem: str = "lecture_notes",
        language: Optional[str] = None,
        domain: str = "",
        intent: str = "",
        on_progress: Optional[Callable[[int, int], None]] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        output_language: str = "en",
    ) -> str:
        """
        Process a lecture video end-to-end and return the path to the PDF.

        Args:
            video_path:  Path to the lecture video file.
            title:       Document title.
            course:      Course name / subtitle.
            output_dir:  Directory for .tex and .pdf output.
            stem:        Base filename (without extension).
            language:    ISO 639-1 code for ASR (e.g. "he"). None = auto-detect.
            domain:      Lecture subject area, e.g. "Linear Algebra".
            intent:      Short focus instruction from the user, e.g. "Emphasise proofs".
            output_language: Short code for the language the finished notes are
                         WRITTEN in ("en", "ar", "he", …); independent of `language`
                         (the spoken/ASR language). Unknown codes fall back to
                         English. Drives both the generation prompt and the LaTeX
                         preamble (direction, script font, localized labels).
            system_prompt: Custom top-level generation instruction. When provided it
                         REPLACES the built-in lecture note-taker system prompt (and the
                         domain block), enabling non-lecture categories. The synthesis
                         step still appends the per-segment transcript/visual data and
                         the pipeline still emits LaTeX → PDF + .tex.
            user_prompt: Custom task-specific instruction used in place of the built-in
                         per-segment synthesis template. Only used when system_prompt is
                         also provided.
        """
        start_wall = time.time()
        body = self.summarize_body(
            video_path=video_path,
            title=title,
            output_dir=output_dir,
            language=language,
            domain=domain,
            intent=intent,
            on_progress=on_progress,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_language=output_language,
        )

        pdf_path = build_and_compile(
            body=body, title=title, course=course,
            output_dir=output_dir, stem=stem,
            output_language=self._output_language,
        )

        print("\n" + "=" * 60)
        print(f"Done in {time.time() - start_wall:.0f}s")
        print(f"PDF: {pdf_path}")
        print("=" * 60)
        return pdf_path

    def summarize_body(
        self,
        video_path: str,
        title: str = "Lecture Notes",
        output_dir: str = "output",
        language: Optional[str] = None,
        domain: str = "",
        intent: str = "",
        on_progress: Optional[Callable[[int, int], None]] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        output_language: str = "en",
        figure_prefix: str = "",
    ) -> str:
        """Run the full understanding pipeline and return the LaTeX BODY, without
        compiling a document. `summarize` wraps this with `build_and_compile`;
        multi-video jobs (videoarm.core.multi_summarizer) call it per video and
        stitch the bodies into one document.

        `figure_prefix` namespaces the figure files copied into
        `<output_dir>/figures/` so several videos sharing an output_dir cannot
        overwrite each other's screenshots (segment numbering restarts per video).
        """
        self._language    = language
        self._domain      = domain.strip()
        self._intent      = intent.strip()
        self._output_dir  = output_dir
        self._figure_prefix = figure_prefix
        self._custom_user_prompt = (user_prompt or "").strip() or None
        # Task context handed to the figure selector on custom jobs so frame
        # choice matches the caller's document, not the lecture rubric.
        _task_bits = [(system_prompt or "").strip(), (user_prompt or "").strip()]
        self._custom_task_context = "\n\n".join(b for b in _task_bits if b)[:800] or None

        lang_spec = resolve(output_language)
        self._output_language = lang_spec.code
        directive = _language_directive(lang_spec)

        if system_prompt and system_prompt.strip():
            # Custom mode: caller supplies the full generation instruction. Do not
            # mix in the lecture note-taker prompt or the domain block. Only enforce
            # the output language when the caller explicitly asked for a non-English
            # one — otherwise the caller's own prompt controls the language.
            self._system_prompt = system_prompt
            if lang_spec.code != "en":
                self._system_prompt += "\n\n" + directive
        else:
            domain_block = (
                f"\n\nLECTURE DOMAIN: {self._domain}\n"
                "Apply domain-appropriate notation, terminology, and mathematical rigour. "
                "Use the standard conventions of this field for symbols and definitions."
                if self._domain else ""
            )
            self._system_prompt = _SYSTEM_NOTE_TAKER + "\n\n" + directive + domain_block
        print("=" * 60)
        print("VideoARM — Lecture Summarizer")
        print("=" * 60)
        print(f"  Video    : {video_path}")
        print(f"  Title    : {title}")
        print(f"  Domain   : {self._domain or '(not specified)'}")
        print(f"  Intent   : {self._intent or '(not specified)'}")
        print(f"  Language : {language or 'auto-detect'} (ASR)")
        print(f"  Output   : {lang_spec.name} [{lang_spec.code}]"
              f"{' — RTL' if lang_spec.rtl else ''}")
        print(f"  Visual   : {self.VISUAL_FPS:.0f} fps  "
              f"({self.VISUAL_SUBSEG_SECS}s sub-windows, "
              f"{self.VISUAL_GRID_ROWS}×{self.VISUAL_GRID_COLS} grids)")
        print(f"  Output   : {output_dir}/")
        print("=" * 60)

        start_wall = time.time()

        self.agent._initialize_video(video_path)
        self.agent.session_id   = str(int(time.time() * 1000))[-8:]
        self.agent.hm3          = self.agent._empty_hm3()
        self.agent.video_has_audio = True

        video_info = self.agent.video_info
        segments   = self._plan_segments(video_info)

        if on_progress:
            on_progress(0, len(segments))

        frames_per_seg = int(self.VISUAL_FPS * self.SEGMENT_SECS)
        print(f"\nSegments  : {len(segments)} × {self.SEGMENT_SECS//60}-min chunks")
        print(f"Frames/seg: ~{frames_per_seg} "
              f"@ {self.VISUAL_FPS:.0f} fps  "
              f"→ {frames_per_seg // (self.VISUAL_GRID_ROWS * self.VISUAL_GRID_COLS)} "
              f"grid images across "
              f"{self.SEGMENT_SECS // self.VISUAL_SUBSEG_SECS} sub-windows\n")

        # Process segments concurrently — each is independent and the heavy work
        # is GPU model calls the vLLM backend batches happily. Results are
        # reassembled in document order regardless of completion order; progress
        # fires per completion. SEGMENT_CONCURRENCY=1 restores sequential order.
        n_segs = len(segments)
        latex_sections: List[str] = [""] * n_segs
        done_count = 0

        def _run_segment(idx: int, seg: Dict[str, Any]) -> str:
            t0, t1 = seg["start_time"], seg["end_time"]
            print(f"┌─ Segment {idx + 1}/{n_segs} "
                  f"({t0:.0f}s–{t1:.0f}s, {(t1-t0)/60:.1f} min) ─────────")
            tex = self._process_segment(video_path, seg, video_info, idx + 1, n_segs)
            print(f"└─ Segment {idx + 1}/{n_segs} done ({len(tex)} chars of LaTeX)\n")
            return tex

        workers = max(1, min(self.SEGMENT_CONCURRENCY, n_segs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_segment, idx, seg): idx
                for idx, seg in enumerate(segments)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                latex_sections[idx] = fut.result()
                done_count += 1
                if on_progress:
                    on_progress(done_count, n_segs)

        body    = "\n\n".join(latex_sections)
        elapsed = time.time() - start_wall
        print(f"All segments processed in {elapsed:.0f}s\n")

        self.agent._cleanup_temp_frames()
        return body

    # ------------------------------------------------------------------ #
    # Segment planning                                                     #
    # ------------------------------------------------------------------ #

    def _plan_segments(self, video_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        fps          = video_info["fps"]
        total_frames = video_info["total_frames"]
        seg_frames   = int(self.SEGMENT_SECS * fps)
        overlap_frames = int(self.OVERLAP_SECS * fps)

        segments: List[Dict[str, Any]] = []
        start = 0
        while start < total_frames:
            end = min(start + seg_frames - 1, total_frames - 1)
            segments.append({
                "start_frame": start,
                "end_frame":   end,
                "start_time":  start / fps,
                "end_time":    end   / fps,
                "duration":    (end - start) / fps,
            })
            if end >= total_frames - 1:
                break
            start = end - overlap_frames + 1
        return segments

    def _plan_visual_subsegs(
        self, seg: Dict[str, Any], fps: float
    ) -> List[Dict[str, Any]]:
        """Split a segment into 1-minute sub-windows for dense frame sampling."""
        sub_frames = int(self.VISUAL_SUBSEG_SECS * fps)
        start = seg["start_frame"]
        end   = seg["end_frame"]
        subs: List[Dict[str, Any]] = []
        while start <= end:
            sub_end = min(start + sub_frames - 1, end)
            subs.append({
                "start_frame": start,
                "end_frame":   sub_end,
                "start_time":  start   / fps,
                "end_time":    sub_end / fps,
                "duration":    (sub_end - start + 1) / fps,
            })
            if sub_end >= end:
                break
            start = sub_end + 1
        return subs

    # ------------------------------------------------------------------ #
    # Per-segment processing                                               #
    # ------------------------------------------------------------------ #

    def _process_segment(
        self,
        video_path: str,
        seg: Dict[str, Any],
        video_info: Dict[str, Any],
        seg_num: int,
        total_segs: int,
    ) -> str:
        _t = {}
        _s = time.time()
        transcript   = self._transcribe_segment(
            video_path, seg, video_info,
            language=getattr(self, "_language", None),
        )
        _t["transcribe"] = time.time() - _s; _s = time.time()
        visual_notes, n_subsegs = self._extract_visual_content(
            video_path, seg, video_info,
        )
        _t["visual"] = time.time() - _s; _s = time.time()
        figures = self._select_key_figures(video_path, seg, seg_num)
        _t["figures"] = time.time() - _s; _s = time.time()
        latex = self._synthesise_latex_section(
            seg, transcript, visual_notes, n_subsegs, seg_num, total_segs, figures,
        )
        _t["synthesis"] = time.time() - _s; _s = time.time()
        if not latex:
            return _fallback_section(seg, transcript, visual_notes)
        out = self._postprocess_latex(latex, seg_num, total_segs)
        _t["postprocess"] = time.time() - _s
        print("│  ⏱  seg %d timing: %s" % (
            seg_num, "  ".join(f"{k}={v:.0f}s" for k, v in _t.items())))
        return out

    # ------------------------------------------------------------------ #
    # Step 1 — Audio transcription                                        #
    # ------------------------------------------------------------------ #

    def _transcribe_segment(
        self,
        video_path: str,
        seg: Dict[str, Any],
        video_info: Dict[str, Any],  # noqa: ARG002
        language: Optional[str] = None,
    ) -> str:
        print("│  [1/3] Transcribing audio …")
        try:
            result = self.agent._audio_transcriber(
                video_path=video_path,
                frame_ranges=[{
                    "start_frame": seg["start_frame"],
                    "end_frame":   seg["end_frame"],
                }],
                reason="Lecture audio transcription",
                language=language,
            )
            if result.get("status") == "no_audio":
                print("│       No audio stream found.")
                return ""
            text = result.get("transcript_text", "").strip()
            print(f"│       {len(text)} chars transcribed.")
            return text
        except Exception as exc:
            print(f"│  ⚠  Transcription failed: {exc}")
            return ""

    # ------------------------------------------------------------------ #
    # Step 2 — Visual content extraction (4 fps, 1-min sub-windows)       #
    # ------------------------------------------------------------------ #

    def _extract_visual_content(
        self,
        video_path: str,
        seg: Dict[str, Any],
        video_info: Dict[str, Any],
    ) -> tuple:
        """Return (combined_visual_notes: str, n_subsegs: int)."""
        fps     = video_info["fps"]
        subsegs = self._plan_visual_subsegs(seg, fps)
        n_sub   = len(subsegs)
        print(f"│  [2/3] Visual extraction — {n_sub} sub-windows "
              f"@ {self.VISUAL_FPS:.0f} fps …")

        model   = self.config.get_model("clip_analyzer")
        api_key, base_url = self.config.get_api_config("clip_analyzer")
        params  = {
            **self.config.get_model_params("clip_analyzer"),
            "max_tokens": 600,   # concise bullets per batch; 48 batches × 600 ≈ 28k chars → truncated to 18k for synthesis
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }

        # Per-segment temp namespace so concurrent segments never share frame
        # files (named by global index → collide across resolutions/segments).
        # Prefixed with the base session id so _cleanup_temp_frames still removes it.
        sess = f"{self.agent.session_id}_v{seg['start_frame']}"

        # Phase 1 (sequential, CPU): sample frames + build grids for every
        # sub-window, flattening into independent vision-model tasks.
        bs = self.VISUAL_BATCH_SIZE
        tasks: List[Dict[str, Any]] = []
        for i, sub in enumerate(subsegs, 1):
            n_frames = max(1, int(self.VISUAL_FPS * sub["duration"]))
            try:
                frame_paths = self.agent._extract_frames_proportional(
                    video_path=video_path,
                    frame_ranges=[{
                        "start_frame": sub["start_frame"],
                        "end_frame":   sub["end_frame"],
                    }],
                    total_frames=n_frames,
                    target_short_side=256,
                    silent=True,
                    session_id=sess,
                )
            except Exception as exc:
                print(f"│    ⚠  sub-window {i} frame extraction failed: {exc}")
                continue
            if not frame_paths:
                continue

            composites = self.agent._make_composite_grids(
                frame_paths,
                rows=self.VISUAL_GRID_ROWS,
                cols=self.VISUAL_GRID_COLS,
            )
            image_list = [str(p) for p in composites] if composites else frame_paths
            user_prompt = _VISUAL_EXTRACTION_USER.format(
                start_time=sub["start_time"],
                end_time=sub["end_time"],
                sub_idx=i,
                total_sub=n_sub,
            )
            for batch in (image_list[j:j+bs] for j in range(0, len(image_list), bs)):
                tasks.append({
                    "sub": i,
                    "start": sub["start_time"],
                    "end": sub["end_time"],
                    "batch": batch,
                    "prompt": user_prompt,
                })

        # Phase 2 (parallel, GPU): the vision calls are independent; run them
        # concurrently and let vLLM batch them. pool.map preserves input order,
        # so notes stay in sub-window → batch order.
        def _call(task: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
            try:
                response = self.agent._run_with_retry(
                    lambda: call_openai_model_with_tools(
                        messages=[
                            {"role": "system", "content": _VISUAL_EXTRACTION_SYSTEM},
                            {"role": "user",   "content": task["prompt"]},
                        ],
                        model_name=model,
                        endpoints=base_url,
                        api_key=api_key,
                        image_paths=task["batch"],
                        **params,
                    )
                )
                return task, (response or {}).get("content", "").strip()
            except Exception as exc:
                print(f"│    ⚠  sub-window {task['sub']} batch failed: {exc}")
                return task, ""

        # Accumulate batch outputs per sub-window, preserving order.
        subs_acc: Dict[int, Dict[str, Any]] = {}
        if tasks:
            workers = max(1, min(self.VISUAL_CONCURRENCY, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for task, content in pool.map(_call, tasks):
                    entry = subs_acc.setdefault(
                        task["sub"], {"start": task["start"], "end": task["end"], "parts": []}
                    )
                    if content:
                        entry["parts"].append(content)

        all_notes: List[str] = []
        for i in sorted(subs_acc):
            entry = subs_acc[i]
            if not entry["parts"]:
                continue
            combined_sub = "\n".join(entry["parts"])
            all_notes.append(f"[{entry['start']:.0f}s–{entry['end']:.0f}s]\n{combined_sub}")
            print(f"│    sub-window {i}/{n_sub}: {len(combined_sub)} chars")

        combined = "\n\n".join(all_notes)
        print(f"│       Total visual notes: {len(combined)} chars")
        return combined, n_sub

    # ------------------------------------------------------------------ #
    # Step 3 — LaTeX synthesis                                            #
    # ------------------------------------------------------------------ #

    def _synthesise_latex_section(
        self,
        seg: Dict[str, Any],
        transcript: str,
        visual_notes: str,
        n_subsegs: int,
        seg_num: int,
        total_segs: int,
        figures: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        print("│  [3/3] Synthesising LaTeX …")

        # Truncate inputs to keep synthesis prompt well within the 32k context window.
        # Reserve ~12k tokens for output; remaining ~20k tokens ≈ 80k chars for inputs.
        MAX_VISUAL  = 18_000
        MAX_AUDIO   = 5_000
        visual_in   = visual_notes[:MAX_VISUAL] if len(visual_notes) > MAX_VISUAL else visual_notes
        if len(visual_notes) > MAX_VISUAL:
            visual_in += "\n… [visual notes truncated — see raw frames for remainder]"
        audio_in    = transcript[:MAX_AUDIO]  if len(transcript)    > MAX_AUDIO  else transcript

        if figures:
            fig_lines = "\n".join(
                f"  {f['path']} — \"{f['caption']}\""
                for f in figures
            )
            figures_block = (
                "Place each figure below where contextually appropriate:\n" + fig_lines
            )
        else:
            figures_block = "(no figures selected for this segment)"

        custom_user = getattr(self, "_custom_user_prompt", None)
        if custom_user:
            user_prompt = custom_user + _CUSTOM_DATA_BLOCK.format(
                seg_num=seg_num,
                total_segs=total_segs,
                start_time=seg["start_time"],
                end_time=seg["end_time"],
                duration=seg["duration"] / 60,
                n_subsegs=n_subsegs,
                transcript=audio_in or "(no audio available)",
                visual_notes=visual_in or "(no visual content available)",
                figures_block=figures_block,
            )
        else:
            user_prompt = _SEGMENT_SYNTHESIS_USER.format(
                duration=seg["duration"] / 60,
                seg_num=seg_num,
                total_segs=total_segs,
                start_time=seg["start_time"],
                end_time=seg["end_time"],
                n_subsegs=n_subsegs,
                transcript=audio_in or "(no audio available)",
                visual_notes=visual_in or "(no visual content available)",
                figures_block=figures_block,
            )

        model   = self.config.get_model("controller")
        api_key, base_url = self.config.get_api_config("controller")
        params  = {
            **self.config.get_model_params("controller"),
            "max_tokens": 12000,
            "temperature": 0.1,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }

        system_msg = getattr(self, "_system_prompt", None) or _SYSTEM_NOTE_TAKER
        intent     = getattr(self, "_intent", "")
        final_user = (
            user_prompt + f"\n\nUSER FOCUS: {intent}"
            if intent else user_prompt
        )

        try:
            response = self.agent._run_with_retry(
                lambda: call_openai_model_with_tools(
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": final_user},
                    ],
                    model_name=model,
                    endpoints=base_url,
                    api_key=api_key,
                    **params,
                )
            )
            content = (response or {}).get("content", "").strip()
            print(f"│       {len(content)} chars of LaTeX generated.")
            return content
        except Exception as exc:
            print(f"│  ⚠  LaTeX synthesis failed: {exc}")
            return ""


    # ------------------------------------------------------------------ #
    # Figure selection                                                    #
    # ------------------------------------------------------------------ #

    def _select_key_figures(
        self,
        video_path: str,
        seg: Dict[str, Any],
        seg_num: int,
    ) -> List[Dict[str, str]]:
        """Sample frames from a segment, ask the vision model which are figure-worthy,
        copy selected frames to output_dir/figures/, return list of {path, caption}."""
        print(f"│  [fig] Selecting figures for segment {seg_num} …")
        output_dir = getattr(self, "_output_dir", "output")
        figures_dir = Path(output_dir) / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)

        try:
            frame_paths = self.agent._extract_frames_proportional(
                video_path=video_path,
                frame_ranges=[{
                    "start_frame": seg["start_frame"],
                    "end_frame":   seg["end_frame"],
                }],
                total_frames=self.FIGURE_SAMPLES,
                target_short_side=480,
                silent=True,
                # Own namespace: figure frames (480px) must not collide with the
                # visual frames (256px) of this or any concurrent segment.
                session_id=f"{self.agent.session_id}_g{seg['start_frame']}",
            )
        except Exception as exc:
            print(f"│  ⚠  Figure frame extraction failed: {exc}")
            return []

        if not frame_paths:
            return []

        n = len(frame_paths)
        task_context = getattr(self, "_custom_task_context", None)
        if task_context:
            system_msg = _FIGURE_SELECTION_SYSTEM_CUSTOM.format(task_context=task_context)
            user_msg = _FIGURE_SELECTION_USER_CUSTOM.format(
                start_time=seg["start_time"],
                end_time=seg["end_time"],
                n_frames=n,
                last_idx=n - 1,
            )
        else:
            system_msg = _FIGURE_SELECTION_SYSTEM
            user_msg = _FIGURE_SELECTION_USER.format(
                start_time=seg["start_time"],
                end_time=seg["end_time"],
                n_frames=n,
                last_idx=n - 1,
            )

        model     = self.config.get_model("clip_analyzer")
        api_key, base_url = self.config.get_api_config("clip_analyzer")
        params    = {
            **self.config.get_model_params("clip_analyzer"),
            "max_tokens": 400,
            "temperature": 0.0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }

        try:
            response = self.agent._run_with_retry(
                lambda: call_openai_model_with_tools(
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": user_msg},
                    ],
                    model_name=model,
                    endpoints=base_url,
                    api_key=api_key,
                    image_paths=[str(p) for p in frame_paths],
                    **params,
                )
            )
            raw = (response or {}).get("content", "").strip()
        except Exception as exc:
            print(f"│  ⚠  Figure selection model call failed: {exc}")
            return []

        # Parse JSON — tolerate markdown fences
        try:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            selections = json.loads(m.group(0)) if m else []
        except Exception:
            print(f"│  ⚠  Figure selection JSON parse failed: {raw[:120]}")
            return []

        results: List[Dict[str, str]] = []
        for item in selections[: self.FIGURE_MAX]:
            idx = item.get("frame_index")
            caption = item.get("caption", "").strip()
            if not isinstance(idx, int) or not (0 <= idx < n) or not caption:
                continue
            src = Path(str(frame_paths[idx]))
            prefix = getattr(self, "_figure_prefix", "")
            dest_name = f"{prefix}seg{seg_num:02d}_fig{len(results)}{src.suffix}"
            dest = figures_dir / dest_name
            shutil.copy2(src, dest)
            rel_path = f"figures/{dest_name}"
            results.append({"path": rel_path, "caption": caption})
            print(f"│       Figure {len(results)}: {rel_path} — {caption[:60]}")

        if not results:
            print(f"│       No figure-worthy frames found (model said: {raw[:160]!r})")
        return results

    # ------------------------------------------------------------------ #
    # Post-processing — LaTeX cleanup                                     #
    # ------------------------------------------------------------------ #

    def _postprocess_latex(self, latex: str, seg_num: int, total_segs: int) -> str:
        """Send a LaTeX section through the LLM to fix typos, broken envs, and arrangement."""
        print(f"│  [post] Cleaning LaTeX for segment {seg_num}/{total_segs} …")
        if not latex.strip():
            return latex

        model     = self.config.get_model("controller")
        api_key, base_url = self.config.get_api_config("controller")
        params    = {
            **self.config.get_model_params("controller"),
            "max_tokens": 14000,
            "temperature": 0.0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }

        user_msg = _POSTPROCESS_USER.format(latex=latex)
        try:
            response = self.agent._run_with_retry(
                lambda: call_openai_model_with_tools(
                    messages=[
                        {"role": "system", "content": _POSTPROCESS_SYSTEM},
                        {"role": "user",   "content": user_msg},
                    ],
                    model_name=model,
                    endpoints=base_url,
                    api_key=api_key,
                    **params,
                )
            )
            cleaned = (response or {}).get("content", "").strip()
            if cleaned:
                print(f"│       Post-processed: {len(latex)} → {len(cleaned)} chars")
                return cleaned
        except Exception as exc:
            print(f"│  ⚠  Post-processing failed: {exc} — keeping original")
        return latex


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback_section(seg: Dict, transcript: str, visual_notes: str = "") -> str:  # noqa: ARG001
    """Minimal LaTeX placeholder when synthesis fails — keeps document compilable."""
    t0, t1 = seg["start_time"], seg["end_time"]
    snippet = transcript[:500].replace("&", r"\&").replace("%", r"\%").replace("#", r"\#") if transcript else ""
    return (
        f"\\subsection{{Lecture Content ({t0:.0f}s\\,--\\,{t1:.0f}s)}}\n\n"
        f"\\begin{{note}}\n"
        f"Automated synthesis failed for this segment. "
        f"Raw transcript excerpt:\n\\end{{note}}\n\n"
        + (f"\\begin{{quote}}{snippet}\\end{{quote}}\n" if snippet else "")
    )
