#!/usr/bin/env python3
"""
Regression test for the `output_language` feature (written-notes language,
separate from the spoken/ASR `language`).

Covers every layer that does NOT need the GPU pipeline or a live video:

  1. Request parsing         — `output_language` survives JSON body parsing on
                               /v1/summarize, /youtube, /custom (real HTTP calls).
  2. Case-insensitivity      — HE/he/AR/ar normalise; unknown codes fall back to en.
  3. Persistence             — the normalised code is stored in SQLite and read back.
  4. No conflation           — `language` (ASR) and `output_language` (notes) stay
                               independent (e.g. language=en, output_language=ar).
  5. Prompt construction     — the LLM directive says "Write ALL notes in <Lang>".
  6. Rendered TeX            — build_latex_document for he/ar contains Hebrew/Arabic
                               characters and an RTL (polyglossia/bidi) preamble.
  7. PDF generation          — build_and_compile for he/ar produces a valid PDF.

This is offline and fast — the video pipeline (executor.submit) is stubbed so no
real download/GPU job runs. The live end-to-end proof lives in the smoke test.

Usage:
    python test_output_language.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

# Isolate DB + output dir BEFORE importing the server (paths are read at import).
_TMP = tempfile.mkdtemp(prefix="ol_test_")
os.environ["VIDEOARM_DB_PATH"]    = str(Path(_TMP) / "jobs.db")
os.environ["VIDEOARM_OUTPUT_DIR"] = _TMP
os.environ.setdefault("VIDEOARM_API_KEY", "test-key")

_FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    line = f"  {mark}  {label}"
    if detail:
        line += f"  [{detail}]"
    print(line)
    if not cond:
        _FAILURES.append(label)


HEBREW = re.compile(r"[֐-׿]")
ARABIC = re.compile(r"[؀-ۿ]")


# ── 2 + 5: normalisation + prompt directive (pure functions) ──────────────────
def test_normalisation_and_directive() -> None:
    print("\n[normalisation + directive]")
    from videoarm.latex.languages import normalize_code, resolve
    from videoarm.core.lecture_summarizer import _language_directive

    for code in ("HE", "he", "hE"):
        check(normalize_code(code) == "he", f"normalize_code({code!r}) -> 'he'")
    for code in ("AR", "ar"):
        check(normalize_code(code) == "ar", f"normalize_code({code!r}) -> 'ar'")
    check(normalize_code("EN") == "en", "normalize_code('EN') -> 'en'")
    check(normalize_code("zz") == "en", "unknown code 'zz' falls back to 'en'")
    check(normalize_code("") == "en", "empty code falls back to 'en'")

    check(resolve("he").rtl and resolve("ar").rtl, "Hebrew & Arabic marked RTL")
    check(not resolve("en").rtl, "English marked LTR")

    he_dir = _language_directive(resolve("he"))
    ar_dir = _language_directive(resolve("ar"))
    check("Write ALL notes in Hebrew" in he_dir, "directive instructs 'Write ALL notes in Hebrew'")
    check("Write ALL notes in Arabic" in ar_dir, "directive instructs 'Write ALL notes in Arabic'")
    check("right-to-left" in he_dir.lower(), "Hebrew directive mentions right-to-left")


# ── 6 + 7: renderer TeX + PDF ────────────────────────────────────────────────
def test_renderer_and_pdf() -> None:
    print("\n[renderer TeX + PDF]")
    from videoarm.latex.renderer import build_latex_document, build_and_compile

    bodies = {
        "he": r"\section{מבוא}\nפסקה בעברית עם מתמטיקה $a^2+b^2=c^2$.",
        "ar": r"\section{مقدمة}\nفقرة بالعربية مع رياضيات $E=mc^2$.",
    }
    pat = {"he": HEBREW, "ar": ARABIC}
    outdir = tempfile.mkdtemp(prefix="ol_render_")

    for code in ("he", "ar"):
        doc = build_latex_document(bodies[code], title="T", course="", author="",
                                   output_language=code)
        low = doc.lower()
        rtl = ("polyglossia" in low) or ("bidi" in low) or ("rtl" in low)
        check(rtl, f"[{code}] TeX preamble is RTL (polyglossia/bidi)")
        check(bool(pat[code].search(doc)), f"[{code}] generated TeX contains script chars",
              f"{len(pat[code].findall(doc))} chars")

        try:
            pdf = build_and_compile(body=bodies[code], title="T", course="", author="",
                                    output_dir=outdir, stem=f"t_{code}", output_language=code)
            ok = bool(pdf) and Path(pdf).exists() and Path(pdf).stat().st_size > 0
            hdr = ok and Path(pdf).read_bytes()[:4] == b"%PDF"
            check(hdr, f"[{code}] PDF compiled with valid header",
                  f"{Path(pdf).stat().st_size if ok else 0} bytes")
        except Exception as exc:  # e.g. xelatex missing on a dev box
            check(False, f"[{code}] PDF compiled", f"error: {exc}")


# ── 1 + 3 + 4: HTTP parsing, persistence, no-conflation (executor stubbed) ────
def test_endpoints_parse_and_persist() -> None:
    print("\n[HTTP parsing + persistence]")
    import videoarm.api.server as server
    from starlette.testclient import TestClient

    # Stub the pool so no real download/GPU job runs; we only assert the row.
    class _Noop:
        def submit(self, *a, **k):
            return None
    server._executor = _Noop()

    client = TestClient(server.app)
    hdr = {"X-API-Key": os.environ["VIDEOARM_API_KEY"]}

    def stored_output_language(job_id: str):
        with server._db() as conn:
            row = conn.execute(
                "SELECT language, output_language FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return (row["language"], row["output_language"]) if row else (None, None)

    vid = "https://example.com/clip.mp4"

    # /v1/summarize — omitted language (auto), HE output, lowercase to prove casing.
    r = client.post("/v1/summarize", headers=hdr,
                    json={"video_url": vid, "output_language": "he"})
    check(r.status_code == 202, "/v1/summarize accepts output_language", f"HTTP {r.status_code}")
    lang, out = stored_output_language(r.json()["job_id"])
    check(out == "he", "/v1/summarize persists output_language='he'", f"got {out!r}")
    check(lang is None, "/v1/summarize leaves ASR language None on auto", f"got {lang!r}")

    # /v1/summarize — language=en + output_language=AR (uppercase): no conflation.
    r = client.post("/v1/summarize", headers=hdr,
                    json={"video_url": vid, "language": "en", "output_language": "AR"})
    lang, out = stored_output_language(r.json()["job_id"])
    check((lang, out) == ("en", "ar"),
          "en audio -> ar notes stays distinct (no conflation, casing)", f"got {(lang, out)}")

    # /v1/summarize/youtube
    r = client.post("/v1/summarize/youtube", headers=hdr,
                    json={"url": "https://youtu.be/dQw4w9WgXcQ", "output_language": "HE"})
    check(r.status_code == 202, "/youtube accepts output_language", f"HTTP {r.status_code}")
    _, out = stored_output_language(r.json()["job_id"])
    check(out == "he", "/youtube persists output_language='he'", f"got {out!r}")

    # /v1/summarize/custom
    r = client.post("/v1/summarize/custom", headers=hdr,
                    json={"source": {"kind": "url", "url": vid},
                          "output_language": "ar",
                          "system_prompt": "You output LaTeX body only.",
                          "user_prompt": "Write notes."})
    check(r.status_code == 202, "/custom accepts output_language", f"HTTP {r.status_code}")
    _, out = stored_output_language(r.json()["job_id"])
    check(out == "ar", "/custom persists output_language='ar'", f"got {out!r}")

    # Unknown code over HTTP must not 422 — falls back to English.
    r = client.post("/v1/summarize", headers=hdr,
                    json={"video_url": vid, "output_language": "zz"})
    check(r.status_code == 202, "unknown output_language does not 422", f"HTTP {r.status_code}")
    _, out = stored_output_language(r.json()["job_id"])
    check(out == "en", "unknown output_language stored as 'en'", f"got {out!r}")


def main() -> int:
    print("━" * 64)
    print("  VideoARM — output_language regression test")
    print("━" * 64)
    test_normalisation_and_directive()
    test_renderer_and_pdf()
    test_endpoints_parse_and_persist()

    print("\n" + "━" * 64)
    if _FAILURES:
        print(f"  FAILED — {len(_FAILURES)} check(s):")
        for f in _FAILURES:
            print(f"    ✗ {f}")
        print("━" * 64)
        return 1
    print("  ALL CHECKS PASSED")
    print("━" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
