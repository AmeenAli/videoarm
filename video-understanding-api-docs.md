# Jard.ai Video Understanding API — Developer Reference

> **Product:** Jard.ai — AI-powered lecture and video understanding  
> **Model backend:** Qwen3.6-35B-A3B (vllm) + Qwen3-ASR-1.7B  
> **Output:** Structured LaTeX lecture notes compiled to PDF  
> **API style:** Asynchronous job queue — submit → poll → download

---

## Table of Contents

1. [Overview](#overview)
2. [Base Endpoint](#base-endpoint)
3. [Authentication](#authentication)
4. [Job Lifecycle](#job-lifecycle)
5. [File Attachment Methods](#file-attachment-methods)
6. [Single vs Multiple Files](#single-vs-multiple-files)
7. [File Management Endpoints](#file-management-endpoints)
8. [Request & Response Shapes](#request--response-shapes)
9. [Limits](#limits)
10. [Error Cases](#error-cases)
11. [AI SDK Integration](#ai-sdk-integration)
12. [End-to-End Example](#end-to-end-example)
13. [Best Practices](#best-practices)
14. [Assumptions and Verification Points](#assumptions-and-verification-points)

---

## Overview

Jard.ai turns a lecture or instructional video into dense, professionally typeset PDF lecture notes. You submit a video (by URL or direct upload), the API processes it asynchronously, and you poll for completion before downloading the resulting PDF.

Because video processing is GPU-intensive and takes minutes, the API follows a **job-based pattern** rather than a synchronous request-response pattern. Every response to a submission is `HTTP 202 Accepted` and includes a `job_id` you use to track progress.

---

## Base Endpoint

| Environment | Base URL |
|---|---|
| Production | `http://35.224.182.54:8080` |
| Self-hosted | `http://<your-host>:8080` |

All paths below are relative to the base URL. Every request must include the `X-API-Key` header (see [Authentication](#authentication)).

The OpenAPI/Swagger interactive docs are available at:

```
http://35.224.182.54:8080/docs
```

---

## Authentication

All endpoints require an API key supplied in the `X-API-Key` request header.

```
X-API-Key: 7db96b28bfafe87a851c22000e9758c5
```

There is no `Bearer` prefix and no `Authorization` header — the header name is exactly `X-API-Key`.

### Obtaining an API Key

For self-hosted deployments, set the key via the `VIDEOARM_API_KEY` environment variable before starting the server:

```bash
VIDEOARM_API_KEY=your-secret-key bash start_api.sh
```

The development default key (`7db96b28bfafe87a851c22000e9758c5`) is printed on server startup and **must be rotated before any public deployment**.

### Authentication Errors

| Status | Detail | Meaning |
|---|---|---|
| `401` | `Invalid API key` | Header missing or value incorrect |

---

## Job Lifecycle

```
submit video
     │
     ▼
 [queued]  ──── background worker picks up job
     │
     ▼
[downloading]  ── only for URL-submitted jobs; skipped for uploads
     │
     ▼
[processing]  ── ASR transcription → visual extraction → LaTeX synthesis → PDF compile
     │
     ├──success──▶ [done]    → pdf_url is populated
     └──failure──▶ [failed]  → error message is populated
```

| Status | Description |
|---|---|
| `queued` | Job accepted; waiting for a worker slot |
| `downloading` | Fetching the video from the submitted URL |
| `processing` | Running the full pipeline (ASR + vision + LaTeX) |
| `done` | PDF is ready to download |
| `failed` | Pipeline error; `error` field contains the reason |

Jobs survive server restarts. On startup, any `processing` job is marked `failed` (it was interrupted), and `queued` jobs are automatically re-submitted to the worker pool.

---

## File Attachment Methods

The API accepts video input through three mutually exclusive methods per job. Choose based on where your video lives.

### Method 1 — Direct URL Submission

Send a publicly accessible URL. The API server downloads the file before processing.

**Supported URL types:**

| Type | Example | Notes |
|---|---|---|
| HTTPS direct link | `https://cdn.example.com/lecture.mp4` | Must be a direct download link |
| YouTube URL | `https://www.youtube.com/watch?v=…` | **Use Method 3** (`POST /v1/summarize/youtube`) — this endpoint does a plain HTTP download and cannot resolve YouTube |
| Google Cloud Storage | `gs://my-bucket/lecture.mp4` | Requires GCS credentials on the server |
| Signed URL | `https://storage.googleapis.com/…?X-Goog-Signature=…` | Treated as a plain HTTPS download |

**Endpoint:** `POST /v1/summarize`  
**Content-Type:** `application/json`

```json
{
  "video_url": "https://cdn.example.com/lecture.mp4",
  "title": "Introduction to Quantum Mechanics",
  "domain": "Quantum Physics",
  "intent": "Focus on mathematical derivations",
  "language": "en"
}
```

**cURL:**

```bash
curl -X POST http://35.224.182.54:8080/v1/summarize \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url": "https://cdn.example.com/lecture.mp4",
    "title": "Introduction to Quantum Mechanics",
    "domain": "Quantum Physics",
    "intent": "Focus on mathematical derivations",
    "language": "en"
  }'
```

**Response `202`:**

```json
{
  "job_id": "cc99fe8101",
  "status": "queued",
  "pdf_url": null,
  "error": null
}
```

---

### Method 2 — Direct File Upload

Upload a local video file as multipart form data. The server stores the file in a temporary location for the duration of processing; it is deleted once the job completes (success or failure).

**Endpoint:** `POST /v1/summarize/upload`  
**Content-Type:** `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | binary (file part) | Yes | The video file |
| `title` | string | No | Document title (default: `"Lecture Notes"`) |
| `domain` | string | No | Subject area, e.g. `"Linear Algebra"` |
| `intent` | string | No | Focus instruction, e.g. `"Emphasise proofs"` |
| `language` | string | No | ISO 639-1 code for ASR (`"en"`, `"he"`, …). `null` = auto-detect |

**cURL:**

```bash
curl -X POST http://35.224.182.54:8080/v1/summarize/upload \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -F "file=@/path/to/lecture.mp4" \
  -F "title=Introduction to Quantum Mechanics" \
  -F "domain=Quantum Physics" \
  -F "intent=Focus on mathematical derivations" \
  -F "language=en"
```

**Response `202`:**

```json
{
  "job_id": "a75252a5ec",
  "status": "queued",
  "pdf_url": null,
  "error": null
}
```

> **Note on inline data (base64):** Inline base64-encoded video bodies are not currently supported. Use the upload endpoint for local files. For files larger than 100 MB, prefer the URL method (host the file at an accessible URL) to avoid connection-level timeouts during upload.

---

### Method 3 — YouTube Link

Submit a YouTube URL. The server downloads the video with [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (capped at 720p), then processes it exactly like the other methods. Use this for any `youtube.com` / `youtu.be` link — the plain URL method (Method 1) does a direct HTTP download and cannot resolve YouTube.

**Endpoint:** `POST /v1/summarize/youtube`  
**Content-Type:** `application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | A YouTube video URL |
| `title` | string | No | Document title. **If omitted, the video's own YouTube title is used** |
| `domain` | string | No | Subject area, e.g. `"Linear Algebra"` |
| `intent` | string | No | Focus instruction, e.g. `"Emphasise proofs"` |
| `language` | string | No | ISO 639-1 code for ASR (`"en"`, `"he"`, …). `null` = auto-detect |

```json
{
  "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "domain": "Quantum Physics",
  "intent": "Focus on mathematical derivations",
  "language": "en"
}
```

**cURL:**

```bash
curl -X POST http://35.224.182.54:8080/v1/summarize/youtube \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"
  }'
```

**Response `202`:**

```json
{
  "job_id": "b3f1c0a9de",
  "status": "queued",
  "pdf_url": null,
  "error": null
}
```

> The job passes through a `downloading` status while yt-dlp fetches the video, then `processing`. Poll `GET /v1/jobs/{job_id}` as usual. Private, age-restricted, or region-blocked videos may fail at the download step — the reason appears in the job's `error` field.

---

### Method 4 — Custom Prompts (non-lecture categories)

Process a video with your **own** `system_prompt` and `user_prompt` instead of the built-in lecture note-taker. Use this for categories beyond lectures — tutorials, meetings, cooking, sports, product demos, etc. The video flows through the same understanding pipeline (per-segment audio transcription + visual frame extraction), and the result is still exposed as a **PDF** and raw **LaTeX** at the usual `/pdf` and `/tex` endpoints. Your `system_prompt` should instruct the model to output a LaTeX body so the PDF compiles.

The existing lecture endpoints (Methods 1–3) are **unchanged** and remain the stable default lecture-note API. This endpoint is purely additive.

**Endpoint:** `POST /v1/summarize/custom`  
**Content-Type:** `application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `source` | object | Yes | `{ "kind": "url" \| "youtube", "url": "<video URL>" }`. `kind` is optional — if omitted, YouTube links are auto-detected from the host. `"url"` covers direct, hosted, and signed storage URLs |
| `system_prompt` | string | Yes | Top-level generation instruction (1–20000 chars). Replaces the built-in lecture system prompt |
| `user_prompt` | string | Yes | Task-specific instruction (1–20000 chars). The per-segment transcript + visual notes are appended automatically |
| `title` | string | No | Document title. Defaults to `"Notes"` (or the YouTube title for `kind: "youtube"` when omitted) |
| `category` | string | No | Free-form label (e.g. `"cooking"`), stored with the job for your own bookkeeping. Does not affect generation |
| `language` | string | No | ISO 639-1 code for ASR (`"en"`, `"he"`, …). `null` = auto-detect |

**cURL — direct / hosted URL:**

```bash
curl -X POST http://35.224.182.54:8080/v1/summarize/custom \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "source": { "kind": "url", "url": "https://storage.example.com/clip.mp4" },
    "title": "Knife Skills",
    "category": "cooking",
    "system_prompt": "You are a precise cooking-tutorial note-taker. Output LaTeX body only — no preamble.",
    "user_prompt": "Write step-by-step recipe notes with ingredient lists and timings."
  }'
```

**cURL — YouTube:**

```bash
curl -X POST http://35.224.182.54:8080/v1/summarize/custom \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "source": { "kind": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ" },
    "category": "meeting",
    "system_prompt": "You summarise meetings into LaTeX minutes. Output LaTeX body only.",
    "user_prompt": "Produce minutes: attendees, decisions, and action items."
  }'
```

**Response `202`:** identical [`JobStatus`](#jobstatus-response-for-all-job-endpoints) shape as every other submit endpoint:

```json
{
  "job_id": "a17c0b9e44",
  "status": "queued",
  "pdf_url": null,
  "error": null
}
```

> Poll `GET /v1/jobs/{job_id}` as usual; download the result from `/v1/jobs/{job_id}/pdf` and `/v1/jobs/{job_id}/tex` once `status: "done"`. Validation: a missing, empty, or over-length `system_prompt`/`user_prompt`, or an invalid `source.kind`/`source.url`, returns `422`. Prompt bodies are never written to logs.

---

## Single vs Multiple Files

### Current Behaviour — One File Per Job

**Each job accepts exactly one video file.** You cannot attach multiple video files to a single job submission.

This applies to both input methods:

- `POST /v1/summarize` — one `video_url` per request body
- `POST /v1/summarize/youtube` — one `url` per request body
- `POST /v1/summarize/upload` — one `file` field per multipart form

If you need to process multiple lectures, create one job per video:

```typescript
const jobs = await Promise.all(
  videoUrls.map(url =>
    fetch("http://35.224.182.54:8080/v1/summarize", {
      method: "POST",
      headers: {
        "X-API-Key": "7db96b28bfafe87a851c22000e9758c5",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ video_url: url, title: "Lecture" }),
    }).then(r => r.json())
  )
);
// jobs is now an array of { job_id, status } objects
```

### Model Version Limits Reference

| Model version | Videos per request |
|---|---|
| Jard.ai v1 (current, Qwen3.6-35B-A3B) | **1 video per job** |
| Planned v2 (aligned with Gemini 2.5+) | Up to **10 videos per job** |

> Models equivalent to Gemini 2.0 and earlier support only 1 video per request. Gemini 2.5 Flash and Pro raise this limit to 10. Jard.ai v1 follows the single-video constraint. Multi-video support is on the roadmap for v2.

---

## File Management Endpoints

### Job Listing

Retrieve all jobs for your account, sorted newest first.

**`GET /v1/jobs`**

```bash
curl http://35.224.182.54:8080/v1/jobs \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5"
```

**Response `200`:**

```json
{
  "jobs": [
    {
      "job_id": "cc99fe8101",
      "status": "done",
      "pdf_url": "/v1/jobs/cc99fe8101/pdf",
      "error": null,
      "segments_done": 12,
      "segments_total": 12,
      "estimated_seconds_remaining": null
    },
    {
      "job_id": "a75252a5ec",
      "status": "failed",
      "pdf_url": null,
      "error": "No audio stream found in video",
      "segments_done": 0,
      "segments_total": 0,
      "estimated_seconds_remaining": null
    },
    {
      "job_id": "76fdbcb3ba",
      "status": "processing",
      "pdf_url": null,
      "error": null,
      "segments_done": 4,
      "segments_total": 12,
      "estimated_seconds_remaining": 480
    }
  ]
}
```

---

### Job Status (Poll)

Get the current status of a single job. Poll this endpoint until `status` is `"done"` or `"failed"`.

**`GET /v1/jobs/{job_id}`**

```bash
curl http://35.224.182.54:8080/v1/jobs/cc99fe8101 \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5"
```

**Response `200` (in progress, first segment still running):**

```json
{
  "job_id": "cc99fe8101",
  "status": "processing",
  "pdf_url": null,
  "error": null,
  "segments_done": 0,
  "segments_total": 12,
  "estimated_seconds_remaining": null
}
```

**Response `200` (in progress, past first segment):**

```json
{
  "job_id": "cc99fe8101",
  "status": "processing",
  "pdf_url": null,
  "error": null,
  "segments_done": 3,
  "segments_total": 12,
  "estimated_seconds_remaining": 540
}
```

**Response `200` (complete):**

```json
{
  "job_id": "cc99fe8101",
  "status": "done",
  "pdf_url": "/v1/jobs/cc99fe8101/pdf",
  "error": null,
  "segments_done": 12,
  "segments_total": 12,
  "estimated_seconds_remaining": null
}
```

---

### PDF Download

Download the compiled PDF once the job status is `"done"`.

**`GET /v1/jobs/{job_id}/pdf`**

```bash
curl http://35.224.182.54:8080/v1/jobs/cc99fe8101/pdf \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -o lecture_notes.pdf
```

- Returns `application/pdf`
- Filename header: `cc99fe8101.pdf`
- Returns `400` if job is not yet `done`
- Returns `500` if the PDF file is missing on disk (server-side error)

---

### File Retention

Uploaded video files (from `POST /v1/summarize/upload`) are stored temporarily and deleted automatically once the job finishes — whether it succeeds or fails. There is no separate file store or file ID; the file exists only for the duration of its job.

Output PDFs are retained in the job output directory (`VIDEOARM_OUTPUT_DIR`) until the directory is manually cleaned up or the server is reprovisioned. There is currently no API endpoint to delete a specific job or its output PDF.

| Artifact | Retention |
|---|---|
| Uploaded video (temp) | Deleted on job completion |
| Output PDF | Persisted; no automatic expiry |
| Job record (SQLite) | Persisted; no automatic expiry |

> For Gemini File API comparison: Gemini retains uploaded files for **48 hours** and provides `GET`, `LIST`, and `DELETE` operations on the file object. Jard.ai does not currently expose a separate file-object API; uploaded files are tightly coupled to jobs.

---

### Health Check

**`GET /health`** — no authentication required.

```bash
curl http://35.224.182.54:8080/health
```

**Response `200`:**

```json
{ "status": "ok" }
```

---

## Request & Response Shapes

### `SummarizeRequest` (JSON body for `POST /v1/summarize`)

```typescript
interface SummarizeRequest {
  video_url: string;      // Required. Full URL to the video.
  title?: string;         // Default: "Lecture Notes"
  domain?: string;        // Subject area hint, e.g. "Topology"
  intent?: string;        // Focus instruction, e.g. "Emphasise proofs"
  language?: string | null; // ISO 639-1; null = auto-detect
}
```

### `YouTubeRequest` (JSON body for `POST /v1/summarize/youtube`)

```typescript
interface YouTubeRequest {
  url: string;            // Required. A YouTube video URL.
  title?: string;         // If omitted, the video's own YouTube title is used
  domain?: string;        // Subject area hint, e.g. "Topology"
  intent?: string;        // Focus instruction, e.g. "Emphasise proofs"
  language?: string | null; // ISO 639-1; null = auto-detect
}
```

### `CustomRequest` (JSON body for `POST /v1/summarize/custom`)

```typescript
interface CustomRequest {
  source: {
    kind?: "url" | "youtube";  // Optional; omitted → YouTube auto-detected from host
    url: string;               // Direct/hosted/signed video URL, or a YouTube link
  };
  system_prompt: string;       // Required, 1–20000 chars. Replaces the lecture system prompt
  user_prompt: string;         // Required, 1–20000 chars. Task instruction
  title?: string;              // Defaults to "Notes" (or YouTube title for youtube source)
  category?: string;           // Free-form label, stored for bookkeeping; not used in generation
  language?: string | null;    // ISO 639-1; null = auto-detect
}
```

Completed custom jobs expose their PDF and LaTeX at the same `/v1/jobs/{job_id}/pdf` and `/v1/jobs/{job_id}/tex` endpoints and return the identical `JobStatus` shape below.

### `JobStatus` (response for all job endpoints)

```typescript
interface JobStatus {
  job_id: string;           // 10-character hex ID
  status: "queued" | "downloading" | "processing" | "done" | "failed";
  pdf_url: string | null;   // Relative path; non-null only when status === "done"
  error: string | null;     // Non-null only when status === "failed"
  segments_done: number;    // How many 5-minute segments have been fully processed
  segments_total: number;   // Total number of segments planned (set once processing starts)
  estimated_seconds_remaining: number | null; // ETA based on per-segment pace; null until first segment finishes
}
```

### `JobList` (response for `GET /v1/jobs`)

```typescript
interface JobList {
  jobs: JobStatus[];        // Sorted newest first
}
```

---

## Limits

| Limit | Value | Notes |
|---|---|---|
| Videos per job | **1** | Multi-video planned for v2 |
| Max upload size (upload endpoint) | **No hard cap in code** | Network timeout is 600 s; keep files under 2 GB in practice |
| Max URL download timeout | **600 s** | Configured in `httpx.stream` |
| Concurrent jobs processed | **2** (default) | Set by `VIDEOARM_WORKERS` env var |
| Inline base64 video | Not supported | Use URL or upload endpoint |
| Supported video formats | MP4, MOV, AVI, MKV, WebM | Any format readable by OpenCV + ffmpeg |
| ASR audio requirement | Optional | Silent videos processed with visual-only pipeline |
| Language auto-detection | Yes | Whisper-based; override with `language` field |
| Output format | PDF (xelatex) | `.tex` source also written to job output dir |

---

## Error Cases

### HTTP Status Codes

| Status | Endpoint | Meaning |
|---|---|---|
| `401` | All | Missing or invalid `X-API-Key` |
| `400` | `GET /v1/jobs/{id}/pdf` | Job not in `done` state yet |
| `404` | `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/pdf` | `job_id` does not exist |
| `500` | `GET /v1/jobs/{id}/pdf` | PDF file missing on disk despite `done` status |
| `422` | `POST /v1/summarize` | `video_url` not a valid URL |
| `422` | `POST /v1/summarize/youtube` | `url` not a valid URL |
| `422` | `POST /v1/summarize/custom` | Missing/empty/over-length `system_prompt` or `user_prompt`, or invalid `source.kind`/`source.url` |

### Job-Level Errors (in `error` field)

| Error string | Cause |
|---|---|
| `Interrupted by server restart` | Server restarted while job was `processing` |
| `Video file lost on server restart` | Uploaded temp file deleted before job ran |
| `No audio stream found in video` | Video has no audio; transcript will be empty |
| `yt-dlp did not produce a video file` | YouTube download failed (private/age-restricted/region-blocked video, or yt-dlp/format error) |
| Any Python exception string | Unhandled pipeline error; check server logs |

### Error Response Shape

```json
{
  "detail": "Invalid API key"
}
```

For job-level failures, errors surface in the `JobStatus` object, not as an HTTP error:

```json
{
  "job_id": "a75252a5ec",
  "status": "failed",
  "pdf_url": null,
  "error": "xelatex not found. Install TeX Live: sudo apt install texlive-full"
}
```

---

## AI SDK Integration

The [Vercel AI SDK](https://sdk.vercel.ai) (`ai` package) is designed for streaming completion models. Because Jard.ai uses a job-based async pattern, you integrate through a **custom client wrapper** rather than `streamText` or `generateText` directly. The examples below show the recommended pattern.

### Install

```bash
npm install ai
# or
pnpm add ai
```

### Client Configuration

```typescript
// lib/jard.ts
const JARD_BASE_URL = "http://35.224.182.54:8080";
const JARD_API_KEY  = process.env.JARD_API_KEY!; // Set in .env

export interface SubmitOptions {
  videoUrl?: string;
  file?: File | Blob;
  title?: string;
  domain?: string;
  intent?: string;
  language?: string;
}

export interface JobStatus {
  job_id: string;
  status: "queued" | "downloading" | "processing" | "done" | "failed";
  pdf_url: string | null;
  error: string | null;
  segments_done: number;
  segments_total: number;
  estimated_seconds_remaining: number | null;
}
```

### Submitting a Job via URL

```typescript
// lib/jard.ts (continued)

export async function submitByUrl(opts: SubmitOptions): Promise<JobStatus> {
  const res = await fetch(`${JARD_BASE_URL}/v1/summarize`, {
    method: "POST",
    headers: {
      "X-API-Key": JARD_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      video_url: opts.videoUrl,
      title:    opts.title    ?? "Lecture Notes",
      domain:   opts.domain   ?? "",
      intent:   opts.intent   ?? "",
      language: opts.language ?? null,
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`Jard.ai submit error ${res.status}: ${err.detail}`);
  }

  return res.json() as Promise<JobStatus>;
}
```

### Submitting a Job via File Upload

```typescript
// lib/jard.ts (continued)

export async function submitByUpload(
  file: File | Blob,
  opts: Omit<SubmitOptions, "videoUrl" | "file"> = {}
): Promise<JobStatus> {
  const form = new FormData();
  form.append("file", file, (file as File).name ?? "lecture.mp4");
  if (opts.title)    form.append("title",    opts.title);
  if (opts.domain)   form.append("domain",   opts.domain);
  if (opts.intent)   form.append("intent",   opts.intent);
  if (opts.language) form.append("language", opts.language);

  const res = await fetch(`${JARD_BASE_URL}/v1/summarize/upload`, {
    method: "POST",
    headers: {
      "X-API-Key": JARD_API_KEY,
      // Do NOT set Content-Type here; the browser/Node sets it with the boundary
    },
    body: form,
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`Jard.ai upload error ${res.status}: ${err.detail}`);
  }

  return res.json() as Promise<JobStatus>;
}
```

### Polling for Completion

```typescript
// lib/jard.ts (continued)

export async function pollUntilDone(
  jobId: string,
  opts: {
    intervalMs?: number;
    timeoutMs?: number;
    onProgress?: (job: JobStatus) => void;
  } = {}
): Promise<JobStatus> {
  const { intervalMs = 5_000, timeoutMs = 30 * 60_000, onProgress } = opts;
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    const res = await fetch(`${JARD_BASE_URL}/v1/jobs/${jobId}`, {
      headers: { "X-API-Key": JARD_API_KEY },
    });

    if (!res.ok) throw new Error(`Poll error ${res.status}`);

    const job: JobStatus = await res.json();
    onProgress?.(job);

    if (job.status === "done")   return job;
    if (job.status === "failed") throw new Error(`Job failed: ${job.error}`);

    await new Promise(r => setTimeout(r, intervalMs));
  }

  throw new Error(`Job ${jobId} timed out after ${timeoutMs / 1000}s`);
}
```

### Progress Indicator

Use `segments_done`, `segments_total`, and `estimated_seconds_remaining` from any poll response to drive a progress bar in your UI.

```typescript
// React example
const [progress, setProgress] = useState<JobStatus | null>(null);

const done = await pollUntilDone(job.job_id, {
  intervalMs: 8_000,
  onProgress: setProgress,
});

// In JSX:
// {progress && progress.segments_total > 0 && (
//   <ProgressBar
//     value={progress.segments_done / progress.segments_total}
//     label={
//       progress.estimated_seconds_remaining != null
//         ? `~${Math.ceil(progress.estimated_seconds_remaining / 60)} min left`
//         : `${progress.segments_done} / ${progress.segments_total} segments`
//     }
//   />
// )}
```

**How the estimate works:**

The server splits the video into 5-minute segments. `segments_total` is set as soon as processing starts. After each segment finishes, `segments_done` increments and `estimated_seconds_remaining` is recalculated as:

```
elapsed_time / segments_done  ×  (segments_total - segments_done)
```

So the estimate improves in accuracy as more segments complete. It is `null` until the first segment finishes (no data yet) and `null` again when the job is `done`.

**Typical processing times:**

| Video duration | Segments | Approx. wall-clock time |
|---|---|---|
| 15 min | 3 | 6–12 min |
| 30 min | 6 | 12–25 min |
| 60 min | 12 | 25–50 min |
| 90 min | 18 | 40–75 min |

Times depend on GPU load and queue depth. Use `estimated_seconds_remaining` for the most accurate live estimate.

### Downloading the PDF

```typescript
// lib/jard.ts (continued)

export async function downloadPdf(jobId: string): Promise<Blob> {
  const res = await fetch(`${JARD_BASE_URL}/v1/jobs/${jobId}/pdf`, {
    headers: { "X-API-Key": JARD_API_KEY },
  });

  if (!res.ok) throw new Error(`PDF download error ${res.status}`);
  return res.blob();
}
```

### Listing All Jobs

```typescript
// lib/jard.ts (continued)

export async function listJobs(): Promise<JobStatus[]> {
  const res = await fetch(`${JARD_BASE_URL}/v1/jobs`, {
    headers: { "X-API-Key": JARD_API_KEY },
  });

  if (!res.ok) throw new Error(`List jobs error ${res.status}`);
  const body = await res.json() as { jobs: JobStatus[] };
  return body.jobs;
}
```

### Using with AI SDK `generateText` (wrapper pattern)

When you want to integrate Jard.ai into an AI SDK pipeline — for example, a Next.js route that orchestrates multiple AI steps — wrap the job-based flow behind a utility that the rest of your AI SDK code can `await`:

```typescript
// app/api/summarize/route.ts (Next.js App Router)
import { generateText } from "ai";
import { openai } from "@ai-sdk/openai";
import { submitByUrl, pollUntilDone, downloadPdf } from "@/lib/jard";

export async function POST(req: Request) {
  const { videoUrl, question } = await req.json();

  // Step 1 — Generate lecture notes with Jard.ai (job-based)
  const job = await submitByUrl({
    videoUrl,
    title:  "Lecture Notes",
    domain: "Computer Science",
  });

  const done = await pollUntilDone(job.job_id, { intervalMs: 8_000 });
  const pdfBlob = await downloadPdf(done.job_id);

  // Step 2 — Use AI SDK generateText to answer a question about the notes
  // (PDF text extraction step omitted for brevity — extract text from pdfBlob first)
  const notesText = await extractTextFromPdf(pdfBlob); // your PDF-to-text utility

  const { text } = await generateText({
    model: openai("gpt-4o"),
    prompt: `Based on the following lecture notes, answer this question: ${question}\n\n${notesText}`,
  });

  return Response.json({ answer: text, jobId: done.job_id });
}
```

---

## End-to-End Example

This example walks through the full lifecycle: **upload file → wait until ready → poll status → retrieve PDF → clean up**.

```typescript
import { submitByUpload, pollUntilDone, downloadPdf, listJobs } from "@/lib/jard";
import { writeFile } from "fs/promises";

async function processLecture(videoPath: string) {
  // 1. Load video from disk (Node.js)
  const { readFile } = await import("fs/promises");
  const videoBuffer = await readFile(videoPath);
  const videoFile   = new Blob([videoBuffer], { type: "video/mp4" });

  console.log("Uploading video to Jard.ai…");

  // 2. Upload the file and create the job
  //    POST /v1/summarize/upload
  //    X-API-Key: 7db96b28bfafe87a851c22000e9758c5
  const job = await submitByUpload(videoFile, {
    title:    "Quantum Mechanics — Lecture 7",
    domain:   "Quantum Physics",
    intent:   "Emphasise derivations and use cases",
    language: "en",
  });

  console.log(`Job created: ${job.job_id} (status: ${job.status})`);

  // 3. Poll until done (no "wait until ready" step — file is processed immediately)
  //    GET /v1/jobs/{job_id}
  //    X-API-Key: 7db96b28bfafe87a851c22000e9758c5
  console.log("Polling for completion…");
  const done = await pollUntilDone(job.job_id, {
    intervalMs: 8_000,   // check every 8 seconds
    timeoutMs:  30 * 60_000, // give up after 30 minutes
  });

  console.log(`Job done: ${done.job_id} — PDF at ${done.pdf_url}`);

  // 4. Download the resulting PDF
  //    GET /v1/jobs/{job_id}/pdf
  //    X-API-Key: 7db96b28bfafe87a851c22000e9758c5
  const pdfBlob = await downloadPdf(done.job_id);
  const pdfBuffer = Buffer.from(await pdfBlob.arrayBuffer());
  await writeFile(`lecture_notes_${done.job_id}.pdf`, pdfBuffer);

  console.log(`PDF saved: lecture_notes_${done.job_id}.pdf (${pdfBuffer.length} bytes)`);

  // 5. Optionally verify via job list
  //    GET /v1/jobs
  //    X-API-Key: 7db96b28bfafe87a851c22000e9758c5
  const jobs = await listJobs();
  const myJob = jobs.find(j => j.job_id === done.job_id);
  console.log(`Confirmed in job list: ${JSON.stringify(myJob)}`);

  // 6. Clean up
  //    Uploaded video is automatically deleted by the server on job completion.
  //    There is no API call needed to clean up the input file.
  //    Output PDF lives on the server until manually removed (no delete endpoint yet).
  console.log("Input video file was automatically cleaned up by the server.");
}

processLecture("./lecture7.mp4").catch(console.error);
```

### Same flow with cURL (shell script)

```bash
#!/usr/bin/env bash
set -euo pipefail

API_KEY="7db96b28bfafe87a851c22000e9758c5"
BASE="http://35.224.182.54:8080"

# 1. Upload file
echo "Uploading…"
RESPONSE=$(curl -s -X POST "$BASE/v1/summarize/upload" \
  -H "X-API-Key: $API_KEY" \
  -F "file=@lecture7.mp4" \
  -F "title=Quantum Mechanics Lecture 7" \
  -F "domain=Quantum Physics" \
  -F "language=en")

JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Job ID: $JOB_ID"

# 2. Poll until done
while true; do
  STATUS=$(curl -s "$BASE/v1/jobs/$JOB_ID" \
    -H "X-API-Key: $API_KEY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'])")
  echo "Status: $STATUS"
  [[ "$STATUS" == "done" || "$STATUS" == "failed" ]] && break
  sleep 10
done

# 3. Download PDF
if [[ "$STATUS" == "done" ]]; then
  curl -s "$BASE/v1/jobs/$JOB_ID/pdf" \
    -H "X-API-Key: $API_KEY" \
    -o "lecture_notes_${JOB_ID}.pdf"
  echo "Saved: lecture_notes_${JOB_ID}.pdf"
else
  echo "Job failed"
  exit 1
fi
```

---

## Best Practices

**Polling interval:** Poll every 8–15 seconds. A 5-minute lecture takes roughly 3–8 minutes to process. Polling faster than every 5 seconds adds load without meaningful benefit.

**Concurrent submissions:** The server processes up to 2 jobs simultaneously by default. Additional jobs queue automatically — there is no need to throttle submissions from the client side.

**Large files:** For files over 500 MB, prefer the URL method (`POST /v1/summarize`) with the file hosted at an accessible URL. This avoids HTTP upload timeouts for slow connections.

**Silent videos:** Videos without an audio track are processed with visual-only content extraction. You will still receive a PDF, but it may be less detailed. Set `intent` to `"Visual content only — no audio"` to prime the model.

**Language hint:** Providing the `language` field (e.g., `"he"` for Hebrew, `"ar"` for Arabic) significantly improves transcription quality for non-English lectures.

**Domain hint:** The `domain` field injects domain-appropriate mathematical notation and terminology conventions into the prompt. For example, `"Differential Geometry"` will trigger correct use of manifold notation.

**Error recovery:** If a job returns `status: "failed"` with `"Interrupted by server restart"`, simply resubmit the same video — the original uploaded file is gone, but a URL-based job will be re-downloaded automatically on server restart.

**PDF storage:** The server does not currently auto-expire PDFs. Implement a periodic cleanup of `VIDEOARM_OUTPUT_DIR` on the server to prevent disk exhaustion.

---

## Assumptions and Verification Points

The following should be confirmed against the live API before publishing this documentation externally.

| # | Assumption | What to verify |
|---|---|---|
| 1 | Base URL is `http://35.224.182.54:8080` | Confirm the production domain with the deployment team |
| 2 | YouTube URLs work out of the box | Verify `yt-dlp` is installed in the server container/environment |
| 3 | GCS `gs://` URLs work | Verify ADC (Application Default Credentials) are configured on the server |
| 4 | No hard upload size cap | Test with a file >1 GB to confirm no proxy/gateway limit (nginx, load balancer) |
| 5 | Output PDFs have no automatic expiry | Confirm disk retention policy and add a `/v1/jobs/{id}` `DELETE` endpoint if needed |
| 6 | `language: null` triggers auto-detection | Test an English video without the `language` field to confirm ASR still runs |
| 7 | Multiple video support in v2 | No v2 is implemented yet; this is a roadmap item aligned with Gemini 2.5 behaviour |
| 8 | Signed/presigned URLs work | Test with an AWS S3 or GCS signed URL — the 600-second download timeout may expire before the link does |
| 9 | `VIDEOARM_WORKERS=2` is safe for your GPU | Profile VRAM usage with two concurrent jobs; reduce to 1 if OOM errors occur |
| 10 | `.tex` source files contain no sensitive data | The job output directory stores raw `.tex` — confirm it is not world-readable |
