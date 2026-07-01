"""
Output-language registry for lecture-note generation.

A caller picks the language the *finished notes* should be written in with a
short code ("EN", "AR", "HE", …). That choice drives two things:

1. The generation prompt — the model is told to write everything in that language.
2. The LaTeX preamble — the correct polyglossia language, text direction (LTR /
   RTL), script font, and localized structural labels (Theorem, Definition, …,
   Contents) so the PDF actually renders that language.

Everything funnels through :func:`resolve`, which always returns a
:class:`LangSpec`. Unknown / unsupported codes fall back to English so a job can
never fail merely because of an unrecognised language code.

Font choices are validated against what is installed on the server:
  • Arabic  → Amiri        (ships Latin digits, so equation/section numbers render)
  • Hebrew  → David CLM     (Culmus; unlike Noto Serif Hebrew it has Latin digits)
  • CJK     → Noto Sans CJK (best-effort; polyglossia has no CJK support)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Theorem / structural label translations
# ---------------------------------------------------------------------------
#
# Keys are the LaTeX environment names defined in the preamble. Only the
# *display* label changes per language — the environment name the model writes
# (\begin{theorem}) is always English. Any environment missing from a language's
# map falls back to its English label, so a partial translation is safe.

_ENGLISH_LABELS: Dict[str, str] = {
    "theorem":       "Theorem",
    "lemma":         "Lemma",
    "corollary":     "Corollary",
    "proposition":   "Proposition",
    "conjecture":    "Conjecture",
    "claim":         "Claim",
    "definition":    "Definition",
    "example":       "Example",
    "exercise":      "Exercise",
    "problem":       "Problem",
    "algorithm":     "Algorithm",
    "remark":        "Remark",
    "note":          "Note",
    "observation":   "Observation",
    "assumption":    "Assumption",
    "conclusion":    "Conclusion",
    "diagram":       "Diagram",
    "transcription": "Transcription",
    "proof":         "Proof",   # amsthm \proofname
}

_ARABIC_LABELS: Dict[str, str] = {
    "theorem":       "مبرهنة",
    "lemma":         "قضية مساعدة",
    "corollary":     "نتيجة",
    "proposition":   "قضية",
    "conjecture":    "حدسية",
    "claim":         "ادعاء",
    "definition":    "تعريف",
    "example":       "مثال",
    "exercise":      "تمرين",
    "problem":       "مسألة",
    "algorithm":     "خوارزمية",
    "remark":        "ملاحظة",
    "note":          "تنبيه",
    "observation":   "استرصاد",
    "assumption":    "افتراض",
    "conclusion":    "خلاصة",
    "diagram":       "مخطط",
    "transcription": "تفريغ",
    "proof":         "برهان",
}

_HEBREW_LABELS: Dict[str, str] = {
    "theorem":       "משפט",
    "lemma":         "למה",
    "corollary":     "מסקנה",
    "proposition":   "הצעה",
    "conjecture":    "השערה",
    "claim":         "טענה",
    "definition":    "הגדרה",
    "example":       "דוגמה",
    "exercise":      "תרגיל",
    "problem":       "בעיה",
    "algorithm":     "אלגוריתם",
    "remark":        "הערה",
    "note":          "פתק",
    "observation":   "תצפית",
    "assumption":    "הנחה",
    "conclusion":    "סיכום",
    "diagram":       "תרשים",
    "transcription": "תמלול",
    "proof":         "הוכחה",
}


# ---------------------------------------------------------------------------
# LangSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LangSpec:
    """Everything the prompt + renderer need for one output language."""

    code: str                       # normalized 2-letter code, e.g. "ar"
    name: str                       # English display name, e.g. "Arabic"
    polyglossia: Optional[str]      # polyglossia language, None → no polyglossia (CJK)
    rtl: bool = False
    script: Optional[str] = None    # fontspec Script= value for the script font
    script_font: Optional[str] = None   # font family for this language's script
    main_font: Optional[str] = None     # overrides \setmainfont (CJK path)
    main_opts: str = ""             # options for \setmainlanguage[...]
    labels: Dict[str, str] = field(default_factory=dict)  # env -> localized label

    def label(self, env: str) -> str:
        """Localized display label for a theorem-like environment (English fallback)."""
        return self.labels.get(env, _ENGLISH_LABELS.get(env, env.title()))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# RTL languages carry an explicit script font. LTR Latin/Cyrillic/Greek
# languages render with the default Latin Modern font (which covers all three
# scripts), so they only need a polyglossia name.

_RTL = {
    "ar": LangSpec("ar", "Arabic",  "arabic",  rtl=True,
                   script="Arabic", script_font="Amiri",     labels=_ARABIC_LABELS),
    "he": LangSpec("he", "Hebrew",  "hebrew",  rtl=True,
                   script="Hebrew", script_font="David CLM", labels=_HEBREW_LABELS),
    "fa": LangSpec("fa", "Persian", "farsi",   rtl=True,
                   script="Arabic", script_font="Amiri",     labels=_ARABIC_LABELS),
}

# LTR languages that render with the default font. Value = polyglossia name.
_LATIN = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuges",   # polyglossia spells it "portuges"
    "nl": "dutch",
    "sv": "swedish",
    "pl": "polish",
    "cs": "czech",
    "tr": "turkish",
    "ru": "russian",    # Cyrillic — covered by Latin Modern
    "uk": "ukrainian",
    "el": "greek",      # Greek — covered by Latin Modern
}

# CJK: no polyglossia support. Render with Noto Sans CJK as the main font and
# treat the surrounding document as English (no hyphenation applied to CJK).
_CJK = {
    "zh": LangSpec("zh", "Chinese",  None, main_font="Noto Sans CJK SC"),
    "ja": LangSpec("ja", "Japanese", None, main_font="Noto Sans CJK JP"),
    "ko": LangSpec("ko", "Korean",   None, main_font="Noto Sans CJK KR"),
}

# Human-readable names for the Latin set (used in prompts / docs).
_LATIN_NAMES = {
    "en": "English",  "es": "Spanish",  "fr": "French",   "de": "German",
    "it": "Italian",  "pt": "Portuguese", "nl": "Dutch",  "sv": "Swedish",
    "pl": "Polish",   "cs": "Czech",    "tr": "Turkish",  "ru": "Russian",
    "uk": "Ukrainian", "el": "Greek",
}

# Common aliases → canonical code.
_ALIASES = {
    "eng": "en", "english": "en",
    "arb": "ar", "arabic": "ar",
    "heb": "he", "hebrew": "he", "iw": "he",   # "iw" is the legacy ISO code for Hebrew
    "per": "fa", "persian": "fa", "farsi": "fa",
    "spa": "es", "spanish": "es",
    "fra": "fr", "fre": "fr", "french": "fr",
    "ger": "de", "deu": "de", "german": "de",
    "por": "pt", "portuguese": "pt",
    "rus": "ru", "russian": "ru",
    "gre": "el", "ell": "el", "greek": "el",
    "chi": "zh", "zho": "zh", "chinese": "zh", "zh-cn": "zh", "zh-hans": "zh",
    "jpn": "ja", "japanese": "ja",
    "kor": "ko", "korean": "ko",
}

DEFAULT_CODE = "en"

_ENGLISH_SPEC = LangSpec("en", "English", "english")


def _latin_spec(code: str) -> LangSpec:
    return LangSpec(code, _LATIN_NAMES.get(code, code.upper()), _LATIN[code])


def normalize_code(code: Optional[str]) -> str:
    """Map an arbitrary user-supplied language value to a canonical code.

    Case/whitespace-insensitive; accepts ISO 639-1 codes, a few 639-2/3 codes,
    English names, and common locale forms ("zh-CN"). Unknown → 'en'."""
    if not code:
        return DEFAULT_CODE
    c = code.strip().lower().replace("_", "-")
    if c in _ALIASES:
        c = _ALIASES[c]
    if c in _RTL or c in _LATIN or c in _CJK:
        return c
    # Locale like "fr-FR" — take the primary subtag and retry.
    if "-" in c:
        return normalize_code(c.split("-", 1)[0])
    return DEFAULT_CODE


def resolve(code: Optional[str]) -> LangSpec:
    """Return a LangSpec for any input, defaulting to English. Never raises."""
    c = normalize_code(code)
    if c in _RTL:
        return _RTL[c]
    if c in _CJK:
        return _CJK[c]
    if c == "en":
        return _ENGLISH_SPEC
    if c in _LATIN:
        return _latin_spec(c)
    return _ENGLISH_SPEC


def is_supported(code: Optional[str]) -> bool:
    """True if the code maps to a specific (non-fallback) supported language."""
    if not code:
        return False
    c = code.strip().lower().replace("_", "-")
    c = _ALIASES.get(c, c)
    return c in _RTL or c in _LATIN or c in _CJK or (
        "-" in c and is_supported(c.split("-", 1)[0])
    )


def supported_codes() -> Dict[str, str]:
    """code -> English name, for docs / help text."""
    out: Dict[str, str] = {}
    for code, spec in _RTL.items():
        out[code] = spec.name
    for code in _LATIN:
        out[code] = _LATIN_NAMES.get(code, code.upper())
    for code, spec in _CJK.items():
        out[code] = spec.name
    return out
