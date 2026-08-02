"""
Semantic-description endpoints — raw timestamped perception, no reasoning.

POST /v1/describe               Submit a video (direct URL or YouTube) for
                                perception-only processing → JSON timeline
GET  /v1/jobs/{id}/semantic     Download the finished JSON document

The result is a machine-consumable timeline for downstream agents: per-window
transcript utterances, objective visual observations, and optional keyframe
screenshots — every element carries start/end timestamps in seconds. Keyframe
image files are served by the existing /v1/jobs/{id}/images endpoints.

Isolated module like multi.py: server.py mounts the router and dispatches
restart recovery; shared infra is imported lazily from videoarm.api.server
(a top-level back-import would be circular).
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

router = APIRouter()

# Timeline resolution bounds (seconds per window).
MIN_WINDOW_SECS = 10
MAX_WINDOW_SECS = 300


def _require_key(x_api_key: str = Header()) -> None:
    from videoarm.api import server as srv  # noqa: PLC0415
    if x_api_key != srv.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class DescribeSource(BaseModel):
    kind: Optional[Literal["url", "youtube"]] = None  # auto-detected if omitted
    url:  HttpUrl


class DescribeRequest(BaseModel):
    """Perception-only job: what is seen and heard, with timestamps — no
    analysis, no reasoning, no PDF. Output is JSON for downstream agents."""
    source:      DescribeSource
    title:       Optional[str] = None
    language:    Optional[str] = None   # spoken-language hint for ASR
    window_secs: int = Field(default=60, ge=MIN_WINDOW_SECS, le=MAX_WINDOW_SECS)
    keyframes:   bool = True


@router.post("/v1/describe", status_code=202)
def submit_describe(req: DescribeRequest, _: None = Depends(_require_key)) -> dict:
    """Submit a video for semantic description. Poll /v1/jobs/{job_id}; when
    done, fetch /v1/jobs/{job_id}/semantic (JSON) and the keyframe files via
    /v1/jobs/{job_id}/images.

    Example::

        curl -X POST http://HOST:8080/v1/describe \\
          -H "X-API-Key: $KEY" -H "Content-Type: application/json" \\
          -d '{"source": {"url": "https://youtu.be/xyz"}, "window_secs": 60}'
    """
    import uuid  # noqa: PLC0415

    from videoarm.api import server as srv  # noqa: PLC0415

    url  = str(req.source.url)
    kind = req.source.kind or ("youtube" if srv._is_youtube(url) else "url")
    job_id = uuid.uuid4().hex[:10]
    params = {"window_secs": req.window_secs, "keyframes": req.keyframes}
    srv._insert(job_id, status="queued", source="describe", video_url=url,
                title=req.title or "Semantic Timeline", language=req.language,
                describe_params=json.dumps({**params, "kind": kind}))
    srv._executor.submit(_run_describe_job, job_id, url, kind, req.title,
                         req.language, req.window_secs, req.keyframes)
    return {"job_id": job_id, "status": "queued"}


@router.get("/v1/jobs/{job_id}/semantic")
def download_semantic(job_id: str, _: None = Depends(_require_key)) -> FileResponse:
    """Download the finished semantic timeline (JSON). 400 until the job is done."""
    from videoarm.api import server as srv  # noqa: PLC0415

    row = srv._get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Semantic timeline not ready — job status is '{row['status']}'",
        )
    path = row["semantic_path"]
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404,
                            detail="No semantic timeline for this job")
    return FileResponse(path, media_type="application/json",
                        filename=f"{job_id}.semantic.json")


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_describe_job(
    job_id: str,
    url: str,
    kind: str,
    title: Optional[str],
    language: Optional[str],
    window_secs: int,
    keyframes: bool,
) -> None:
    import shutil  # noqa: PLC0415

    from videoarm.api import server as srv  # noqa: PLC0415

    tmp_video: Optional[str] = None
    dl_dir: Optional[Path] = None
    try:
        srv._set(job_id, status="downloading")
        if kind == "youtube":
            dl_dir = Path(tempfile.mkdtemp(prefix="ytdl_desc_"))
            tmp_video, yt_title = srv._download_youtube(url, dl_dir)
            if not title:
                title = yt_title
                srv._set(job_id, title=yt_title)
        else:
            suffix = Path(url).suffix or ".mp4"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                tmp_video = fh.name
            with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
                r.raise_for_status()
                with open(tmp_video, "wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)

        srv._set(job_id, status="processing", started_at=time.time())

        from videoarm.core.ffmpeg_utils import ensure_decodable  # noqa: PLC0415
        ensure_decodable(tmp_video)

        out_dir = srv.OUTPUT_ROOT / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        from videoarm.core.describer import SemanticDescriber  # noqa: PLC0415

        def _on_progress(done: int, total: int) -> None:
            srv._set(job_id, segments_done=done, segments_total=total)

        doc = SemanticDescriber().describe(
            video_path=tmp_video,
            output_dir=str(out_dir),
            title=title,
            language=language,
            window_secs=window_secs,
            keyframes=keyframes,
            on_progress=_on_progress,
        )
        doc["job_id"] = job_id
        for entry in doc["timeline"]:
            for kf in entry["keyframes"]:
                kf["url"] = f"/v1/jobs/{job_id}/images/{kf['filename']}"

        sem_path = out_dir / f"{job_id}.semantic.json"
        sem_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        srv._set(job_id, status="done", semantic_path=str(sem_path))

    except Exception as exc:
        srv._set(job_id, status="failed", error=str(exc))

    finally:
        if tmp_video and (not dl_dir):
            Path(tmp_video).unlink(missing_ok=True)
        if dl_dir:
            shutil.rmtree(dl_dir, ignore_errors=True)


def resume_describe_job(row) -> None:
    """Re-run a queued describe job after a server restart."""
    try:
        params: Dict[str, Any] = json.loads(row["describe_params"] or "{}")
    except Exception:
        params = {}
    _run_describe_job(
        row["job_id"], row["video_url"],
        params.get("kind") or "url",
        row["title"], row["language"],
        int(params.get("window_secs") or 60),
        bool(params.get("keyframes", True)),
    )
