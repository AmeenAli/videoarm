"""
VideoARM FastAPI server.

Endpoints
---------
POST /v1/summarize          Submit a video URL for processing
POST /v1/summarize/youtube  Submit a YouTube link (downloaded with yt-dlp)
POST /v1/summarize/upload   Upload a local video file
POST /v1/summarize/custom   Submit a video URL (direct or YouTube) with a custom
                            system_prompt + user_prompt (non-lecture categories)
GET  /v1/jobs               List all jobs (newest first)
GET  /v1/jobs/{job_id}      Poll job status
GET  /v1/jobs/{job_id}/pdf  Download the finished PDF
GET  /health                Health check

Auth: X-API-Key header (set VIDEOARM_API_KEY env var, default: "change-me")

Job state is persisted in SQLite (VIDEOARM_DB_PATH) so the queue survives
server restarts. VIDEOARM_WORKERS (default 2) jobs run concurrently so vllm
can batch inference requests across users automatically.
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import List, Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY     = os.getenv("VIDEOARM_API_KEY", "change-me")
OUTPUT_ROOT = Path(os.getenv("VIDEOARM_OUTPUT_DIR", "/tmp/videoarm_jobs"))
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH     = Path(os.getenv("VIDEOARM_DB_PATH", str(OUTPUT_ROOT / "jobs.db")))
MAX_WORKERS = int(os.getenv("VIDEOARM_WORKERS", "2"))

# Upper bound for caller-supplied prompts on /v1/summarize/custom (chars).
MAX_PROMPT_CHARS = int(os.getenv("VIDEOARM_MAX_PROMPT_CHARS", "20000"))

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}


def _is_youtube(url: str) -> bool:
    """True if the URL host is a known YouTube domain (used to auto-route /custom
    when the caller omits source.kind)."""
    try:
        return (urlparse(url).hostname or "").lower() in _YOUTUBE_HOSTS
    except Exception:
        return False


_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


@contextmanager
def _db():
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _init_db() -> None:
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id         TEXT PRIMARY KEY,
                status         TEXT NOT NULL DEFAULT 'queued',
                video_url      TEXT,
                video_path     TEXT,
                source         TEXT NOT NULL DEFAULT 'url',
                title          TEXT NOT NULL DEFAULT 'Lecture Notes',
                language       TEXT,
                domain         TEXT NOT NULL DEFAULT '',
                intent         TEXT NOT NULL DEFAULT '',
                pdf_path       TEXT,
                tex_path       TEXT,
                error          TEXT,
                system_prompt  TEXT,
                user_prompt    TEXT,
                category       TEXT,
                segments_done  INTEGER NOT NULL DEFAULT 0,
                segments_total INTEGER NOT NULL DEFAULT 0,
                started_at     REAL,
                created_at     REAL NOT NULL DEFAULT (unixepoch()),
                updated_at     REAL NOT NULL DEFAULT (unixepoch())
            )
        """)


def _migrate_db() -> None:
    """Add progress columns to existing databases that predate this schema."""
    new_cols = [
        ("segments_done",  "INTEGER NOT NULL DEFAULT 0"),
        ("segments_total", "INTEGER NOT NULL DEFAULT 0"),
        ("started_at",     "REAL"),
        ("tex_path",       "TEXT"),
        ("source",         "TEXT NOT NULL DEFAULT 'url'"),
        ("system_prompt",  "TEXT"),
        ("user_prompt",    "TEXT"),
        ("category",       "TEXT"),
    ]
    with _db() as conn:
        for col, defn in new_cols:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass  # column already exists


def _insert(job_id: str, **fields) -> None:
    fields.setdefault("created_at", time.time())
    fields.setdefault("updated_at", time.time())
    cols         = ", ".join(["job_id"] + list(fields.keys()))
    placeholders = ", ".join("?" * (1 + len(fields)))
    with _db() as conn:
        conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
            [job_id] + list(fields.values()),
        )


def _set(job_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with _db() as conn:
        conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE job_id = ?",
            list(fields.values()) + [job_id],
        )


def _get(job_id: str) -> Optional[sqlite3.Row]:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", [job_id]
        ).fetchone()


def _list_all() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()


def _recover() -> None:
    """On startup: fail interrupted jobs, re-queue surviving queued jobs."""
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'Interrupted by server restart' "
            "WHERE status = 'processing'"
        )
        queued = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued'"
        ).fetchall()

    for row in queued:
        # Custom jobs persist these; lecture jobs leave them NULL (treated as None).
        system_prompt = row["system_prompt"]
        user_prompt   = row["user_prompt"]
        if row["source"] == "youtube" and row["video_url"]:
            _executor.submit(
                _run_job_from_youtube,
                row["job_id"],
                row["video_url"],
                row["title"],
                row["language"],
                row["domain"],
                row["intent"],
                system_prompt,
                user_prompt,
            )
        elif row["video_url"]:
            req = SimpleNamespace(
                video_url=row["video_url"],
                title=row["title"],
                language=row["language"],
                domain=row["domain"],
                intent=row["intent"],
            )
            _executor.submit(_run_job, row["job_id"], req, system_prompt, user_prompt)
        elif row["video_path"] and Path(row["video_path"]).exists():
            _executor.submit(
                _run_job_from_path,
                row["job_id"],
                row["video_path"],
                row["title"],
                row["language"],
                row["domain"],
                row["intent"],
            )
        else:
            _set(row["job_id"],
                 status="failed",
                 error="Video file lost on server restart")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_init_db()
_migrate_db()
app = FastAPI(title="VideoARM", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    _recover()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_key(x_api_key: str = Header()) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SummarizeRequest(BaseModel):
    video_url: HttpUrl
    domain:    str           = ""
    intent:    str           = ""
    title:     str           = "Lecture Notes"
    language:  Optional[str] = None


class YouTubeRequest(BaseModel):
    url:      HttpUrl
    domain:   str           = ""
    intent:   str           = ""
    # Defaults to the YouTube video's own title when left unset.
    title:    Optional[str] = None
    language: Optional[str] = None


class CustomSource(BaseModel):
    # kind is optional — when omitted we auto-detect YouTube from the URL host.
    # An explicit kind always wins. "url" covers direct/hosted/signed video URLs.
    kind: Optional[Literal["url", "youtube"]] = None
    url:  HttpUrl


class CustomRequest(BaseModel):
    """Prompt-configurable job. The video flows through the same direct-URL or
    YouTube pipeline as the lecture endpoints; only the generation prompts differ."""
    source:        CustomSource
    title:         Optional[str] = None
    language:      Optional[str] = None
    category:      Optional[str] = None  # free-form label, stored for record-keeping
    system_prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    user_prompt:   str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)


class JobStatus(BaseModel):
    job_id:                      str
    status:                      str   # queued | downloading | processing | done | failed
    pdf_url:                     Optional[str] = None
    tex_url:                     Optional[str] = None
    error:                       Optional[str] = None
    segments_done:               int   = 0
    segments_total:              int   = 0
    estimated_seconds_remaining: Optional[int] = None


class JobList(BaseModel):
    jobs: List[JobStatus]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_status(row: sqlite3.Row) -> JobStatus:
    est = None
    done  = row["segments_done"]
    total = row["segments_total"]
    if row["status"] == "processing" and done > 0 and total > 0:
        started_at = row["started_at"]
        if started_at:
            elapsed = time.time() - started_at
            secs_per_seg = elapsed / done
            est = int(secs_per_seg * (total - done))

    is_done = row["status"] == "done"
    return JobStatus(
        job_id=row["job_id"],
        status=row["status"],
        pdf_url=f"/v1/jobs/{row['job_id']}/pdf" if is_done else None,
        tex_url=f"/v1/jobs/{row['job_id']}/tex" if is_done else None,
        error=row["error"],
        segments_done=done,
        segments_total=total,
        estimated_seconds_remaining=est,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/summarize", response_model=JobStatus, status_code=202)
def submit(req: SummarizeRequest, _: None = Depends(_require_key)) -> JobStatus:
    """Submit a video URL for lecture-note generation. Returns a job_id to poll."""
    job_id = uuid.uuid4().hex[:10]
    _insert(job_id, status="queued", video_url=str(req.video_url),
            title=req.title, language=req.language,
            domain=req.domain, intent=req.intent)
    _executor.submit(_run_job, job_id, req)
    return JobStatus(job_id=job_id, status="queued")


@app.post("/v1/summarize/youtube", response_model=JobStatus, status_code=202)
def submit_youtube(req: YouTubeRequest, _: None = Depends(_require_key)) -> JobStatus:
    """Submit a YouTube link for lecture-note generation. The video is downloaded
    server-side with yt-dlp. If `title` is omitted the video's own title is used.
    Returns a job_id to poll."""
    job_id = uuid.uuid4().hex[:10]
    fields = dict(status="queued", source="youtube", video_url=str(req.url),
                  language=req.language, domain=req.domain, intent=req.intent)
    if req.title:  # otherwise let the DB default stand until yt-dlp resolves it
        fields["title"] = req.title
    _insert(job_id, **fields)
    _executor.submit(_run_job_from_youtube, job_id, str(req.url),
                     req.title, req.language, req.domain, req.intent)
    return JobStatus(job_id=job_id, status="queued")


@app.post("/v1/summarize/upload", response_model=JobStatus, status_code=202)
async def submit_upload(
    file:     UploadFile,
    domain:   str           = Form(default=""),
    intent:   str           = Form(default=""),
    title:    str           = Form(default="Lecture Notes"),
    language: Optional[str] = Form(default=None),
    _: None = Depends(_require_key),
) -> JobStatus:
    """Upload a local video file for lecture-note generation. Returns a job_id to poll."""
    suffix = Path(file.filename or "video").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        tmp_path = fh.name
        while chunk := await file.read(1 << 20):
            fh.write(chunk)

    job_id = uuid.uuid4().hex[:10]
    _insert(job_id, status="queued", video_path=tmp_path,
            title=title, language=language, domain=domain, intent=intent)
    _executor.submit(_run_job_from_path, job_id, tmp_path, title, language, domain, intent)
    return JobStatus(job_id=job_id, status="queued")


@app.post("/v1/summarize/custom", response_model=JobStatus, status_code=202)
def submit_custom(req: CustomRequest, _: None = Depends(_require_key)) -> JobStatus:
    """Submit a video for **prompt-configurable** processing — supports categories
    beyond lectures by supplying an explicit `system_prompt` (top-level generation
    instruction) and `user_prompt` (task instruction). The video itself flows through
    the same pipeline as the lecture endpoints, and the result is exposed as PDF
    (`/v1/jobs/{id}/pdf`) and raw LaTeX (`/v1/jobs/{id}/tex`), exactly like the
    default lecture jobs.

    `source.url` may be a direct/hosted/signed video URL **or** a YouTube link. Set
    `source.kind` to `"url"` or `"youtube"` to choose the path explicitly; if omitted,
    YouTube links are auto-detected from the URL host. Returns a `job_id` to poll.

    Example — direct/hosted URL::

        curl -X POST http://HOST:8080/v1/summarize/custom \\
          -H "X-API-Key: $VIDEOARM_API_KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
                "source": {"kind": "url", "url": "https://storage.example.com/clip.mp4"},
                "title": "Knife Skills",
                "category": "cooking",
                "system_prompt": "You are a precise cooking-tutorial note-taker. Output LaTeX body only.",
                "user_prompt": "Write step-by-step recipe notes with ingredient lists and timings."
              }'

    Example — YouTube::

        curl -X POST http://HOST:8080/v1/summarize/custom \\
          -H "X-API-Key: $VIDEOARM_API_KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
                "source": {"kind": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ"},
                "category": "meeting",
                "system_prompt": "You summarise meetings into LaTeX minutes. Output LaTeX body only.",
                "user_prompt": "Produce minutes: attendees, decisions, action items."
              }'
    """
    job_id = uuid.uuid4().hex[:10]
    url    = str(req.source.url)
    is_yt  = req.source.kind == "youtube" or (req.source.kind is None and _is_youtube(url))
    title  = req.title or "Notes"
    base   = dict(language=req.language, category=req.category,
                  system_prompt=req.system_prompt, user_prompt=req.user_prompt)

    if is_yt:
        fields = dict(base, status="queued", source="youtube", video_url=url)
        if req.title:  # else let the DB default stand until yt-dlp resolves the title
            fields["title"] = title
        _insert(job_id, **fields)
        _executor.submit(_run_job_from_youtube, job_id, url, req.title,
                         req.language, "", "", req.system_prompt, req.user_prompt)
    else:
        _insert(job_id, status="queued", source="url", video_url=url,
                title=title, **base)
        sreq = SimpleNamespace(video_url=url, title=title, language=req.language,
                               domain="", intent="")
        _executor.submit(_run_job, job_id, sreq, req.system_prompt, req.user_prompt)

    return JobStatus(job_id=job_id, status="queued")


@app.get("/v1/jobs", response_model=JobList)
def list_jobs(_: None = Depends(_require_key)) -> JobList:
    """List all jobs, newest first."""
    return JobList(jobs=[_row_to_status(r) for r in _list_all()])


@app.get("/v1/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, _: None = Depends(_require_key)) -> JobStatus:
    """Poll the status of a submitted job."""
    row = _get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_status(row)


@app.get("/v1/jobs/{job_id}/pdf")
def download_pdf(job_id: str, _: None = Depends(_require_key)) -> FileResponse:
    """Download the finished PDF. Returns 400 if the job is not done yet."""
    row = _get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "done":
        raise HTTPException(
            status_code=400,
            detail=f"PDF not ready — job status is '{row['status']}'",
        )
    pdf_path = row["pdf_path"]
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=500, detail="PDF file missing on disk")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"{job_id}.pdf")


@app.get("/v1/jobs/{job_id}/tex")
def download_tex(job_id: str, _: None = Depends(_require_key)) -> FileResponse:
    """Download the LaTeX source (.tex). Returns 400 if the job is not done yet."""
    row = _get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "done":
        raise HTTPException(
            status_code=400,
            detail=f"LaTeX not ready — job status is '{row['status']}'",
        )
    tex_path = row["tex_path"]
    if not tex_path or not Path(tex_path).exists():
        raise HTTPException(status_code=500, detail="LaTeX file missing on disk")
    return FileResponse(tex_path, media_type="text/x-tex", filename=f"{job_id}.tex")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _process(
    job_id: str,
    video_path: str,
    title: str,
    language: Optional[str],
    domain: str,
    intent: str,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
) -> None:
    try:
        _set(job_id, status="processing", started_at=time.time())
        out_dir = OUTPUT_ROOT / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        from videoarm.core.lecture_summarizer import LectureSummarizer  # noqa: PLC0415
        summarizer = LectureSummarizer()

        def _on_progress(done: int, total: int) -> None:
            _set(job_id, segments_done=done, segments_total=total)

        pdf_path = summarizer.summarize(
            video_path=video_path,
            title=title,
            output_dir=str(out_dir),
            stem=job_id,
            language=language,
            domain=domain,
            intent=intent,
            on_progress=_on_progress,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        tex_path = pdf_path.replace(".pdf", ".tex") if pdf_path else None
        _set(job_id, status="done", pdf_path=pdf_path, tex_path=tex_path)

    except Exception as exc:
        _set(job_id, status="failed", error=str(exc))


def _run_job(
    job_id: str,
    req,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
) -> None:
    tmp_video: Optional[str] = None
    try:
        _set(job_id, status="downloading")
        suffix = Path(str(req.video_url)).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            tmp_video = fh.name

        with httpx.stream("GET", str(req.video_url), follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp_video, "wb") as fh:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)

        _process(job_id, tmp_video, req.title, req.language, req.domain, req.intent,
                 system_prompt, user_prompt)

    except Exception as exc:
        _set(job_id, status="failed", error=str(exc))

    finally:
        if tmp_video and Path(tmp_video).exists():
            Path(tmp_video).unlink(missing_ok=True)


def _download_youtube(url: str, dest_dir: Path) -> tuple[str, Optional[str]]:
    """Download a YouTube video into dest_dir with yt-dlp.

    Returns (path_to_downloaded_file, video_title). Caps resolution at 720p to
    keep downloads small — frame sampling never needs more than that.
    """
    import yt_dlp  # noqa: PLC0415

    ydl_opts = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # requested_downloads[].filepath reflects the final (post-merge) file.
        downloads = info.get("requested_downloads") or []
        path = downloads[0]["filepath"] if downloads else ydl.prepare_filename(info)

    if not path or not Path(path).exists():
        raise RuntimeError("yt-dlp did not produce a video file")
    return path, info.get("title")


def _run_job_from_youtube(
    job_id: str,
    url: str,
    title: Optional[str],
    language: Optional[str],
    domain: str,
    intent: str,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
) -> None:
    dl_dir: Optional[Path] = None
    try:
        _set(job_id, status="downloading")
        dl_dir = Path(tempfile.mkdtemp(prefix="ytdl_"))
        video_path, video_title = _download_youtube(url, dl_dir)

        if not title:  # caller left title unset — use the video's own title
            title = video_title or "Lecture Notes"
            _set(job_id, title=title)

        _process(job_id, video_path, title, language, domain, intent,
                 system_prompt, user_prompt)

    except Exception as exc:
        _set(job_id, status="failed", error=str(exc))

    finally:
        if dl_dir and dl_dir.exists():
            shutil.rmtree(dl_dir, ignore_errors=True)


def _run_job_from_path(
    job_id: str,
    tmp_video: str,
    title: str,
    language: Optional[str],
    domain: str,
    intent: str,
) -> None:
    try:
        _process(job_id, tmp_video, title, language, domain, intent)
    finally:
        if Path(tmp_video).exists():
            Path(tmp_video).unlink(missing_ok=True)
