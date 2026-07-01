"""
LaTeX document builder and PDF compiler for VideoARM lecture notes.

The preamble is assembled per-document so the notes can be typeset in the
caller's chosen output language: the right polyglossia language, text direction
(LTR/RTL), script font, and localized structural labels. See
:mod:`videoarm.latex.languages` for the language registry.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional

from videoarm.latex.languages import LangSpec, resolve

# ---------------------------------------------------------------------------
# Preamble — static blocks (language-independent)
# ---------------------------------------------------------------------------

_PREAMBLE_MATH = r"""
% ── Core math ───────────────────────────────────────────────────────────────
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{amsthm}
\usepackage{mathtools}
"""

_PREAMBLE_LAYOUT = r"""
% ── Page layout ──────────────────────────────────────────────────────────────
\usepackage[margin=1in, top=1.2in, bottom=1.2in]{geometry}
\usepackage{parskip}
\setlength{\parindent}{0pt}

% ── Headers / footers ────────────────────────────────────────────────────────
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\lhead{\small\textit{\nouppercase{\leftmark}}}
\rfoot{\thepage}
\renewcommand{\headrulewidth}{0.4pt}
"""

_PREAMBLE_TAIL = r"""
% ── Code listings ─────────────────────────────────────────────────────────────
\usepackage{listings}
\usepackage{xcolor}
\lstdefinestyle{lecture}{
    basicstyle=\ttfamily\small,
    breaklines=true,
    frame=single,
    numbers=left,
    numberstyle=\tiny\color{gray},
    keywordstyle=\color{blue!70!black}\bfseries,
    commentstyle=\color{green!50!black}\itshape,
    stringstyle=\color{red!60!black},
    showstringspaces=false,
    tabsize=4,
}
\lstset{style=lecture}

% ── Hyperlinks ────────────────────────────────────────────────────────────────
\usepackage[colorlinks=true, linkcolor=blue!70!black,
            citecolor=blue!70!black, urlcolor=blue!70!black]{hyperref}

% ── Graphics / figures ────────────────────────────────────────────────────────
\usepackage{graphicx}
\usepackage{float}

% ── Tables ────────────────────────────────────────────────────────────────────
\usepackage{booktabs}
\usepackage{array}

% ── Enumeration ───────────────────────────────────────────────────────────────
\usepackage{enumitem}
\setlist{nosep, leftmargin=*}

% ── Common math macros ────────────────────────────────────────────────────────
\newcommand{\R}{\mathbb{R}}
\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\C}{\mathbb{C}}
\newcommand{\F}{\mathbb{F}}
\newcommand{\E}{\mathbb{E}}
\newcommand{\Prob}{\mathbb{P}}
\newcommand{\norm}[1]{\left\|#1\right\|}
\newcommand{\abs}[1]{\left|#1\right|}
\newcommand{\inner}[2]{\langle #1,\,#2 \rangle}
\newcommand{\set}[1]{\left\{#1\right\}}
\newcommand{\eps}{\varepsilon}
\newcommand{\given}{\,|\,}
\DeclareMathOperator*{\argmax}{arg\,max}
\DeclareMathOperator*{\argmin}{arg\,min}
\DeclareMathOperator{\rank}{rank}
\DeclareMathOperator{\tr}{tr}
\DeclareMathOperator{\diag}{diag}
\DeclareMathOperator{\Span}{span}
"""


# ---------------------------------------------------------------------------
# Preamble — language-dependent blocks
# ---------------------------------------------------------------------------

# Theorem environments grouped by amsthm style. Order is preserved; "theorem"
# is the base counter (numbered per section) and everything else shares it.
_THM_PLAIN      = ["theorem", "lemma", "corollary", "proposition",
                   "conjecture", "claim"]
_THM_DEFINITION = ["definition", "example", "exercise", "problem", "algorithm"]
_THM_REMARK     = ["remark", "note", "observation", "assumption",
                   "conclusion", "diagram", "transcription"]


def _language_block(spec: LangSpec) -> str:
    """Build the fontspec/polyglossia block for the chosen output language.

    Always makes Latin (English), Arabic (Amiri) and Hebrew (David CLM) available
    so mixed-script content — e.g. an embedded English term or a quoted Hebrew
    phrase — renders in any document. The main language sets the base direction.
    """
    main_poly = spec.polyglossia or "english"   # CJK has no polyglossia language
    # Every document can typeset Latin + Arabic + Hebrew; declare whichever are
    # not the main language as "other" so \textenglish / \textarabic / \texthebrew work.
    others = [lang for lang in ("english", "arabic", "hebrew") if lang != main_poly]

    lines = [
        "% ── Typography (XeLaTeX — Unicode / RTL / CJK aware) ──────────────────────────",
        r"\usepackage{fontspec}",
        r"\usepackage{polyglossia}",
        (f"\\setmainlanguage[{spec.main_opts}]{{{main_poly}}}"
         if spec.main_opts else f"\\setmainlanguage{{{main_poly}}}"),
    ]
    lines += [f"\\setotherlanguage{{{lang}}}" for lang in others]

    # Script fonts. polyglossia's arabic/hebrew require these font families.
    lines.append(r"\newfontfamily\arabicfont[Script=Arabic]{Amiri}")
    lines.append(r"\newfontfamily\hebrewfont[Script=Hebrew]{David CLM}")

    if spec.rtl:
        # Give embedded Latin text a proper Latin font (the RTL script font is
        # used as the document main font otherwise).
        lines.append(r"\newfontfamily\englishfont{Latin Modern Roman}")
    if spec.main_font:
        # CJK path: no polyglossia language exists, so drive the script purely
        # through the main font (Noto Sans CJK covers Latin too).
        lines.append(f"\\setmainfont{{{spec.main_font}}}")

    lines.append(r"\usepackage{microtype}")
    return "\n".join(lines) + "\n"


def _theorem_block(spec: LangSpec) -> str:
    """Build the theorem-environment definitions with localized display labels."""
    out = [
        "% ── Theorem environments ─────────────────────────────────────────────────────",
        r"\theoremstyle{plain}",
        f"\\newtheorem{{theorem}}{{{spec.label('theorem')}}}[section]",
    ]
    out += [f"\\newtheorem{{{env}}}[theorem]{{{spec.label(env)}}}"
            for env in _THM_PLAIN if env != "theorem"]

    out.append(r"\theoremstyle{definition}")
    out += [f"\\newtheorem{{{env}}}[theorem]{{{spec.label(env)}}}"
            for env in _THM_DEFINITION]

    out.append(r"\theoremstyle{remark}")
    out += [f"\\newtheorem{{{env}}}[theorem]{{{spec.label(env)}}}"
            for env in _THM_REMARK]

    # amsthm's proof environment label (polyglossia localizes it for known
    # languages; override so it matches our chosen wording).
    out.append(f"\\renewcommand{{\\proofname}}{{{spec.label('proof')}}}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_latex_document(
    body: str,
    title: str = "Lecture Notes",
    course: str = "",
    author: str = "Jard.ai Automated Notes",
    output_language: str = "en",
) -> str:
    """Return a complete LaTeX document string from body content.

    ``output_language`` is a short code ("en", "ar", "he", …); unknown codes fall
    back to English. It selects the polyglossia language, direction, script font,
    and localized theorem/section labels of the compiled document.
    """
    spec = resolve(output_language)
    title_line = _escape_text(title)
    subtitle_line = (
        f"\\large {_escape_text(course)}" if course else ""
    )
    subtitle_block = (
        f"\\\\\n{subtitle_line}" if subtitle_line else ""
    )
    return (
        r"\documentclass[11pt,letterpaper]{article}" + "\n"
        + _PREAMBLE_MATH
        + _language_block(spec)
        + _PREAMBLE_LAYOUT
        + _theorem_block(spec)
        + _PREAMBLE_TAIL + "\n"
        + r"\begin{document}" + "\n\n"
        + f"\\title{{\\textbf{{{title_line}}}{subtitle_block}}}\n"
        + f"\\author{{{_escape_text(author)}}}\n"
        + r"\date{\today}" + "\n"
        + r"\maketitle" + "\n"
        + r"\tableofcontents" + "\n"
        + r"\newpage" + "\n\n"
        + body.strip() + "\n\n"
        + r"\end{document}" + "\n"
    )


def build_and_compile(
    body: str,
    title: str = "Lecture Notes",
    course: str = "",
    output_dir: str = ".",
    author: str = "Jard.ai Automated Notes",
    stem: str = "lecture_notes",
    output_language: str = "en",
) -> str:
    """
    Build a complete LaTeX document from body content and compile to PDF.

    Args:
        body:       LaTeX body content (sections, math, etc. — no preamble).
        title:      Document title.
        course:     Optional course/subtitle line.
        output_dir: Directory for .tex and .pdf files.
        stem:       Base filename (without extension).
        output_language: Short language code for the compiled notes ("en", "ar",
                    "he", …). Unknown codes fall back to English.

    Returns:
        Absolute path to the generated PDF.
    """
    doc = build_latex_document(body, title, course, author, output_language)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    tex_path = out / f"{stem}.tex"
    tex_path.write_text(doc, encoding="utf-8")
    print(f"LaTeX written: {tex_path}")

    pdf_path = _compile_pdf(tex_path)
    return str(pdf_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _escape_text(text: str) -> str:
    """Escape LaTeX special characters in plain text (NOT in math/LaTeX body)."""
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&",  r"\&"),
        ("%",  r"\%"),
        ("#",  r"\#"),
        ("_",  r"\_"),
        ("{",  r"\{"),
        ("}",  r"\}"),
        ("~",  r"\textasciitilde{}"),
        ("^",  r"\^{}"),
    ]
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


def _compile_pdf(tex_path: Path, runs: int = 2) -> Path:
    """
    Compile a .tex file to PDF using pdflatex.

    Runs pdflatex twice so that cross-references and the table of contents
    are resolved correctly.  Returns path to the PDF.
    """
    cmd = [
        "xelatex",
        "-interaction=nonstopmode",
        f"-output-directory={tex_path.parent}",
        str(tex_path),
    ]

    last_returncode = 0
    for run in range(1, runs + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=180,
                cwd=tex_path.parent,
            )
            last_returncode = proc.returncode
        except FileNotFoundError:
            raise RuntimeError(
                "pdflatex not found. Install TeX Live or MiKTeX:\n"
                "  Ubuntu/Debian: sudo apt install texlive-full\n"
                "  macOS:         brew install mactex"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("pdflatex timed out after 180 s.")

    pdf_path = tex_path.with_suffix(".pdf")
    if not pdf_path.exists():
        log_path = tex_path.with_suffix(".log")
        errors = ""
        if log_path.exists():
            log = log_path.read_bytes().decode("utf-8", errors="replace")
            errors = "\n".join(
                l for l in log.splitlines()
                if l.startswith("!") or "Error" in l
            )[:1000]
        raise RuntimeError(
            f"pdflatex (exit {last_returncode}) produced no PDF.\n{errors}"
        )

    # Clean auxiliary files
    for suffix in (".aux", ".log", ".toc", ".out"):
        aux = tex_path.with_suffix(suffix)
        if aux.exists():
            aux.unlink(missing_ok=True)

    print(f"PDF compiled:  {pdf_path}")
    return pdf_path
