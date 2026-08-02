"""
Multi-video endpoints — submit a STACK of videos as ONE job → ONE combined PDF.

POST /v1/summarize/multi         JSON: list of direct-URL / YouTube sources
POST /v1/summarize/multi/upload  multipart: several `files` parts
POST /v1/summarize/multi/custom  JSON: sources + custom system/user prompts

Both return 202 + a job_id; polling and download use the same /v1/jobs/*
endpoints as single-video jobs. The finished document contains one \\section
per video, in submission order, compiled once (single PDF + .tex).

This module is deliberately isolated from the single-video routes: it owns its
request models, validation, and background worker, and drives the dedicated
``MultiVideoSummarizer`` service — server.py only mounts the router and calls
``resume_multi_job`` during restart recovery. Shared infrastructure (job DB
helpers, the worker executor, the yt-dlp downloader) is imported lazily from
``videoarm.api.server`` inside functions: server.py imports this module, so a
top-level back-import would be circular.
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, HttpUrl

from videoarm.latex.languages import normalize_code

# Upper bound on videos per multi job. Videos are processed sequentially, so a
# large stack mostly means a long job, but each source is also downloaded to
# local disk first — keep the cap modest.
MAX_MULTI_VIDEOS = int(os.getenv("VIDEOARM_MAX_MULTI_VIDEOS", "8"))

# Upper bound for caller-supplied prompts on /multi/custom (chars). Read from the
# env rather than imported from server.py — that back-import is circular.
MAX_PROMPT_CHARS = int(os.getenv("VIDEOARM_MAX_PROMPT_CHARS", "20000"))

router = APIRouter()


def _require_key(x_api_key: str = Header()) -> None:
    from videoarm.api import server as srv  # noqa: PLC0415 — avoid circular import
    if x_api_key != srv.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MultiVideoSource(BaseModel):
    # kind is optional — when omitted, YouTube is auto-detected from the URL host
    # (same rule as /v1/summarize/custom). An explicit kind always wins.
    kind:  Optional[Literal["url", "youtube"]] = None
    url:   HttpUrl
    # Optional per-video heading; becomes this video's \section title in the
    # combined notes. Defaults to the YouTube title (YouTube sources) or "Video i".
    title: Optional[str] = None


class MultiRequest(BaseModel):
    """One job over an ordered stack of videos → one combined document."""
    videos:          List[MultiVideoSource] = Field(min_length=1,
                                                    max_length=MAX_MULTI_VIDEOS)
    title:           str = "Combined Notes"   # title of the combined document
    domain:          str = ""
    intent:          str = ""
    language:        Optional[str] = None      # spoken/ASR language, job-wide
    output_language: str = "en"                # written-notes language, job-wide


class MultiCustomRequest(BaseModel):
    """One job over an ordered stack of videos, generated with the caller's own
    prompts (à la /v1/summarize/custom) → one combined document.

    No `domain`/`intent`: as on the single /custom endpoint, the caller's prompts
    own the instruction space."""
    videos:          List[MultiVideoSource] = Field(min_length=1,
                                                    max_length=MAX_MULTI_VIDEOS)
    system_prompt:   str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    user_prompt:     str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    title:           str = "Combined Notes"   # title of the combined document
    category:        Optional[str] = None     # bookkeeping only, like /custom
    language:        Optional[str] = None     # spoken/ASR language, job-wide
    output_language: str = "en"               # written-notes language, job-wide


class JobAccepted(BaseModel):
    """Submit response — poll /v1/jobs/{job_id} for the full status."""
    job_id: str
    status: str = "queued"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/v1/summarize/multi", response_model=JobAccepted, status_code=202)
def submit_multi(req: MultiRequest, _: None = Depends(_require_key)) -> JobAccepted:
    """Submit an ordered stack of video URLs (direct and/or YouTube, freely mixed)
    to be processed as ONE job producing ONE combined PDF — one \\section per
    video, in the order given. Poll /v1/jobs/{job_id} as usual."""
    from videoarm.api import server as srv  # noqa: PLC0415

    sources: List[Dict[str, Any]] = []
    for v in req.videos:
        url  = str(v.url)
        kind = v.kind or ("youtube" if srv._is_youtube(url) else "url")
        sources.append({"kind": kind, "url": url, "title": v.title})

    job_id   = uuid.uuid4().hex[:10]
    out_lang = normalize_code(req.output_language)
    srv._insert(job_id, status="queued", source="multi", title=req.title,
                language=req.language, output_language=out_lang,
                domain=req.domain, intent=req.intent,
                sources_json=json.dumps(sources))
    srv._executor.submit(_run_multi_job, job_id, sources, req.title,
                         req.language, req.domain, req.intent, out_lang)
    return JobAccepted(job_id=job_id)


@router.post("/v1/summarize/multi/custom", response_model=JobAccepted, status_code=202)
def submit_multi_custom(req: MultiCustomRequest,
                        _: None = Depends(_require_key)) -> JobAccepted:
    """Submit an ordered stack of video URLs (direct and/or YouTube, freely mixed)
    to be processed as ONE job with the caller's own generation prompts →
    ONE combined PDF, one \\section per video in submission order.

    Prompt semantics (same as /v1/summarize/custom, applied to every video):
    `system_prompt` replaces the built-in note-taker instruction; `user_prompt`
    is applied per segment with that segment's transcript / visual notes /
    figures appended automatically. Tell the model to output a LaTeX body only.

    Example::

        curl -X POST http://HOST:8080/v1/summarize/multi/custom \\
          -H "X-API-Key: $VIDEOARM_API_KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
                "videos": [
                  {"url": "https://youtu.be/aaa", "title": "Episode 1"},
                  {"url": "https://cdn.example.com/ep2.mp4", "title": "Episode 2"}
                ],
                "title": "Season Review",
                "category": "video-editing-analysis",
                "system_prompt": "You are a senior film editor... Output LaTeX body only.",
                "user_prompt": "Evaluate pacing, composition, and narrative clarity."
              }'
    """
    from videoarm.api import server as srv  # noqa: PLC0415

    sources: List[Dict[str, Any]] = []
    for v in req.videos:
        url  = str(v.url)
        kind = v.kind or ("youtube" if srv._is_youtube(url) else "url")
        sources.append({"kind": kind, "url": url, "title": v.title})

    job_id   = uuid.uuid4().hex[:10]
    out_lang = normalize_code(req.output_language)
    srv._insert(job_id, status="queued", source="multi", title=req.title,
                language=req.language, output_language=out_lang,
                category=req.category,
                system_prompt=req.system_prompt, user_prompt=req.user_prompt,
                sources_json=json.dumps(sources))
    srv._executor.submit(_run_multi_job, job_id, sources, req.title,
                         req.language, "", "", out_lang,
                         req.system_prompt, req.user_prompt)
    return JobAccepted(job_id=job_id)


@router.post("/v1/summarize/multi/upload", response_model=JobAccepted, status_code=202)
async def submit_multi_upload(
    files:    List[UploadFile],
    title:    str           = Form(default="Combined Notes"),
    domain:   str           = Form(default=""),
    intent:   str           = Form(default=""),
    language: Optional[str] = Form(default=None),
    output_language: str    = Form(default="en"),
    _: None = Depends(_require_key),
) -> JobAccepted:
    """Upload SEVERAL video files in one request (repeat the `files` part) to be
    processed as ONE job producing ONE combined PDF. Part order = section order;
    each file's name (without extension) becomes its section title."""
    from videoarm.api import server as srv  # noqa: PLC0415

    if len(files) > MAX_MULTI_VIDEOS:
        raise HTTPException(
            status_code=422,
            detail=f"Too many videos: {len(files)} > {MAX_MULTI_VIDEOS} "
                   f"(VIDEOARM_MAX_MULTI_VIDEOS)",
        )

    sources: List[Dict[str, Any]] = []
    for f in files:
        suffix = Path(f.filename or "video").suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            tmp_path = fh.name
            while chunk := await f.read(1 << 20):
                fh.write(chunk)
        sources.append({
            "kind":  "file",
            "path":  tmp_path,
            "title": Path(f.filename).stem if f.filename else None,
        })

    job_id   = uuid.uuid4().hex[:10]
    out_lang = normalize_code(output_language)
    srv._insert(job_id, status="queued", source="multi", title=title,
                language=language, output_language=out_lang,
                domain=domain, intent=intent,
                sources_json=json.dumps(sources))
    srv._executor.submit(_run_multi_job, job_id, sources, title,
                         language, domain, intent, out_lang)
    return JobAccepted(job_id=job_id)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_multi_job(
    job_id: str,
    sources: List[Dict[str, Any]],
    title: str,
    language: Optional[str],
    domain: str,
    intent: str,
    output_language: str,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
) -> None:
    """Download every source, then run MultiVideoSummarizer over the stack.

    A failure on ANY video (download, unreadable file, pipeline error) fails the
    whole job with an error naming the offending video — a combined document
    with silently missing parts would be worse than a clean retry."""
    import shutil  # noqa: PLC0415

    from videoarm.api import server as srv  # noqa: PLC0415

    tmp_files: List[str] = []       # local files to delete afterwards
    dl_dirs:   List[Path] = []      # yt-dlp temp dirs to delete afterwards
    try:
        srv._set(job_id, status="downloading")

        videos: List[Dict[str, Any]] = []
        for i, src in enumerate(sources, 1):
            try:
                if src["kind"] == "file":
                    path = src["path"]
                    if not Path(path).exists():
                        raise RuntimeError("uploaded file is missing on disk")
                    tmp_files.append(path)
                    videos.append({"path": path, "title": src.get("title")})

                elif src["kind"] == "youtube":
                    dl_dir = Path(tempfile.mkdtemp(prefix="ytdl_multi_"))
                    dl_dirs.append(dl_dir)
                    path, yt_title = srv._download_youtube(src["url"], dl_dir)
                    videos.append({"path": path,
                                   "title": src.get("title") or yt_title})

                else:  # direct URL — same streaming download as /v1/summarize
                    suffix = Path(src["url"]).suffix or ".mp4"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                        tmp = fh.name
                    tmp_files.append(tmp)
                    with httpx.stream("GET", src["url"], follow_redirects=True,
                                      timeout=600) as r:
                        r.raise_for_status()
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_bytes(chunk_size=1 << 20):
                                fh.write(chunk)
                    videos.append({"path": tmp, "title": src.get("title")})

                from videoarm.core.ffmpeg_utils import ensure_decodable  # noqa: PLC0415
                ensure_decodable(videos[-1]["path"])

            except Exception as exc:
                raise RuntimeError(
                    f"Video {i}/{len(sources)} "
                    f"({src.get('url') or src.get('title') or 'upload'}): {exc}"
                ) from exc

        srv._set(job_id, status="processing", started_at=time.time())
        out_dir = srv.OUTPUT_ROOT / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        from videoarm.core.multi_summarizer import MultiVideoSummarizer  # noqa: PLC0415

        def _on_progress(done: int, total: int) -> None:
            srv._set(job_id, segments_done=done, segments_total=total)

        pdf_path = MultiVideoSummarizer().summarize(
            videos=videos,
            title=title,
            output_dir=str(out_dir),
            stem=job_id,
            language=language,
            domain=domain,
            intent=intent,
            on_progress=_on_progress,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_language=output_language,
        )
        tex_path = pdf_path.replace(".pdf", ".tex") if pdf_path else None
        srv._set(job_id, status="done", pdf_path=pdf_path, tex_path=tex_path)

    except Exception as exc:
        srv._set(job_id, status="failed", error=str(exc))

    finally:
        for p in tmp_files:
            Path(p).unlink(missing_ok=True)
        for d in dl_dirs:
            shutil.rmtree(d, ignore_errors=True)


def resume_multi_job(row) -> None:
    """Re-run a queued multi job after a server restart (called by server._recover).

    URL/YouTube sources are simply re-downloaded; uploaded files must still be
    on disk (temp files usually survive a process restart but not a reboot)."""
    from videoarm.api import server as srv  # noqa: PLC0415

    job_id = row["job_id"]
    try:
        sources = json.loads(row["sources_json"] or "[]")
    except Exception:
        sources = []
    if not sources:
        srv._set(job_id, status="failed",
                 error="Multi-video job lost its source list on server restart")
        return

    lost = [s for s in sources
            if s.get("kind") == "file" and not Path(s.get("path", "")).exists()]
    if lost:
        srv._set(job_id, status="failed",
                 error="Uploaded video files lost on server restart")
        return

    _run_multi_job(job_id, sources, row["title"], row["language"],
                   row["domain"] or "", row["intent"] or "",
                   row["output_language"] or "en",
                   row["system_prompt"], row["user_prompt"])
