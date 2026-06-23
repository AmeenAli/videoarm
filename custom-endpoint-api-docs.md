# Jard.ai — Custom Prompt Endpoint Reference

`POST /v1/summarize/custom`

Process a video with **your own** system prompt and user prompt instead of the
built-in lecture note-taker. Use this for any category beyond lectures — tutorials,
meetings, cooking, sports, product demos, interviews, and so on.

The video flows through the exact same understanding pipeline as the lecture
endpoints (per-segment audio transcription + visual frame extraction). The only
difference is that **your** `system_prompt` drives the top-level generation and
**your** `user_prompt` is the task instruction. The result is still a compiled **PDF**
and raw **LaTeX** (`.tex`), retrievable from the same download endpoints.

> The existing lecture endpoints (`/v1/summarize`, `/v1/summarize/youtube`,
> `/v1/summarize/upload`) are **unchanged** and remain the stable default lecture-note
> API. This endpoint is purely additive — no client migration is required.

---

## Table of Contents

1. [Base URL & Authentication](#base-url--authentication)
2. [How It Works](#how-it-works)
3. [Request](#request)
4. [Validation Rules](#validation-rules)
5. [Response](#response)
6. [Polling for Completion](#polling-for-completion)
7. [Downloading the Result](#downloading-the-result)
8. [cURL Examples](#curl-examples)
9. [TypeScript Example (end-to-end)](#typescript-example-end-to-end)
10. [Error Cases](#error-cases)
11. [Writing a Good System Prompt](#writing-a-good-system-prompt)
12. [Notes & Guarantees](#notes--guarantees)

---

## Base URL & Authentication

**Base URL:** `http://35.224.182.54:8080`

Every request must include the API key in the `X-API-Key` header. There is **no**
`Bearer` prefix and **no** `Authorization` header — the header name is exactly
`X-API-Key`.

```
X-API-Key: 7db96b28bfafe87a851c22000e9758c5
```

A missing or invalid key returns `401 { "detail": "Invalid API key" }`.

---

## How It Works

Video processing is GPU-intensive and takes minutes, so the API is **job-based**:

1. You `POST` a video URL + your prompts → get back a `job_id` immediately (`202`).
2. You poll `GET /v1/jobs/{job_id}` until `status` is `"done"` (or `"failed"`).
3. You download the **PDF** and/or **LaTeX** for the finished job.

Internally each job runs the pipeline in ~5-minute segments. For each segment the
service transcribes the audio and extracts dense visual notes from sampled frames,
then asks the model — using **your** `system_prompt` and `user_prompt` — to write a
LaTeX body for that segment. The per-segment transcript and visual notes are appended
to your `user_prompt` automatically, so you do **not** need to (and cannot) supply the
video content yourself; you only supply the *instructions*. All segments are
concatenated and compiled to a single PDF with XeLaTeX.

Because the output is compiled as LaTeX, **your `system_prompt` should instruct the
model to emit a LaTeX body** (no `\documentclass`, no `\begin{document}`). If the model
emits non-LaTeX, the job may fail at the compile step with the reason in `error`.

---

## Request

**Endpoint:** `POST /v1/summarize/custom`
**Content-Type:** `application/json`

### Body

| Field | Type | Required | Description |
|---|---|---|---|
| `source` | object | **Yes** | The video to process. See [`source`](#source-object) below. |
| `system_prompt` | string | **Yes** | Top-level generation instruction (1–20000 chars). Replaces the built-in lecture system prompt. |
| `user_prompt` | string | **Yes** | Task-specific instruction (1–20000 chars). The per-segment transcript + visual notes are appended to this automatically. |
| `title` | string | No | Document title. Defaults to `"Notes"` — or, for a YouTube source with no title, the video's own YouTube title. |
| `category` | string | No | Free-form label (e.g. `"cooking"`). Stored with the job for your own bookkeeping; it does **not** affect generation. |
| `language` | string \| null | No | ISO 639-1 code for audio transcription (`"en"`, `"he"`, …). `null` = auto-detect. |

### `source` object

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | **Yes** | A direct/hosted/signed video URL **or** a YouTube link. |
| `kind` | `"url"` \| `"youtube"` | No | Which pipeline to use. If omitted, YouTube links are auto-detected from the URL host; everything else is treated as a direct URL. An explicit `kind` always wins. |

- **`kind: "url"`** (or omitted, non-YouTube host) — the server does a direct HTTP
  download of `url`. This is the path for videos your app has already uploaded to
  storage and exposes via a public or signed URL.
- **`kind: "youtube"`** (or omitted, YouTube host) — the server downloads the video
  with `yt-dlp` (capped at 720p), then processes it identically.

### Example body

```json
{
  "source": { "kind": "url", "url": "https://storage.example.com/clip.mp4" },
  "title": "Knife Skills",
  "category": "cooking",
  "language": "en",
  "system_prompt": "You are a precise cooking-tutorial note-taker. Output LaTeX body only — no preamble.",
  "user_prompt": "Write step-by-step recipe notes with ingredient lists and timings."
}
```

---

## Validation Rules

Invalid requests return `422` (Unprocessable Entity) before any job is created:

| Condition | Result |
|---|---|
| `system_prompt` missing, empty, or > 20000 chars | `422` |
| `user_prompt` missing, empty, or > 20000 chars | `422` |
| `source.kind` is not `"url"` or `"youtube"` | `422` |
| `source.url` is not a valid URL | `422` |
| `source` missing entirely | `422` |

The 20000-character cap is configurable server-side via the `VIDEOARM_MAX_PROMPT_CHARS`
environment variable.

---

## Response

On success the endpoint returns **`202 Accepted`** with a `JobStatus` object — the
**same shape** returned by every other submit endpoint:

```json
{
  "job_id": "a17c0b9e44",
  "status": "queued",
  "pdf_url": null,
  "error": null
}
```

### `JobStatus` shape

```typescript
interface JobStatus {
  job_id: string;           // 10-character hex ID
  status: "queued" | "downloading" | "processing" | "done" | "failed";
  pdf_url: string | null;   // Relative path; non-null only when status === "done"
  tex_url: string | null;   // Relative path; non-null only when status === "done"
  error: string | null;     // Non-null only when status === "failed"
  segments_done: number;    // Segments fully processed so far
  segments_total: number;   // Total segments planned (set once processing starts)
  estimated_seconds_remaining: number | null; // ETA; null until the first segment finishes
}
```

### Status lifecycle

```
queued → downloading → processing → done
                                  ↘ failed
```

---

## Polling for Completion

```
GET /v1/jobs/{job_id}
X-API-Key: <key>
```

Returns the current `JobStatus`. Poll every few seconds. When `status` is `"processing"`,
use `segments_done` / `segments_total` and `estimated_seconds_remaining` to render a
progress bar. When `status` is `"done"`, `pdf_url` and `tex_url` become non-null.

---

## Downloading the Result

Once `status` is `"done"`, both artifacts are available (both require `X-API-Key`):

| Endpoint | Content-Type | Description |
|---|---|---|
| `GET /v1/jobs/{job_id}/pdf` | `application/pdf` | Compiled PDF |
| `GET /v1/jobs/{job_id}/tex` | `text/x-tex` | Raw LaTeX source (closest thing to plain text) |

- `400` if the job is not yet `done`.
- `404` if the `job_id` does not exist.
- `500` if the file is missing on disk despite a `done` status.

---

## cURL Examples

### Submit — direct / hosted URL

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

### Submit — YouTube

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

### Poll

```bash
curl http://35.224.182.54:8080/v1/jobs/a17c0b9e44 \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5"
```

### Download PDF and LaTeX

```bash
curl http://35.224.182.54:8080/v1/jobs/a17c0b9e44/pdf \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -o notes.pdf

curl http://35.224.182.54:8080/v1/jobs/a17c0b9e44/tex \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -o notes.tex
```

---

## TypeScript Example (end-to-end)

```typescript
const BASE = "http://35.224.182.54:8080";
const API_KEY = process.env.JARD_API_KEY!;
const headers = { "X-API-Key": API_KEY, "Content-Type": "application/json" };

// 1. Submit
async function submitCustom(): Promise<string> {
  const res = await fetch(`${BASE}/v1/summarize/custom`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      source: { kind: "url", url: "https://storage.example.com/clip.mp4" },
      title: "Knife Skills",
      category: "cooking",
      system_prompt:
        "You are a precise cooking-tutorial note-taker. Output LaTeX body only.",
      user_prompt:
        "Write step-by-step recipe notes with ingredient lists and timings.",
    }),
  });
  if (res.status !== 202) throw new Error(`Submit failed: ${res.status}`);
  const job = await res.json();
  return job.job_id as string;
}

// 2. Poll until done
async function waitForJob(jobId: string): Promise<any> {
  while (true) {
    const res = await fetch(`${BASE}/v1/jobs/${jobId}`, {
      headers: { "X-API-Key": API_KEY },
    });
    const job = await res.json();
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(`Job failed: ${job.error}`);
    await new Promise((r) => setTimeout(r, 5000));
  }
}

// 3. Download
async function downloadPdf(jobId: string): Promise<Buffer> {
  const res = await fetch(`${BASE}/v1/jobs/${jobId}/pdf`, {
    headers: { "X-API-Key": API_KEY },
  });
  return Buffer.from(await res.arrayBuffer());
}

const jobId = await submitCustom();
await waitForJob(jobId);
const pdf = await downloadPdf(jobId);
```

---

## Error Cases

### HTTP status codes

| Status | When | Meaning |
|---|---|---|
| `202` | Submit succeeded | Job queued; poll with the returned `job_id` |
| `401` | Any request | Missing or invalid `X-API-Key` |
| `422` | Submit | Invalid body — see [Validation Rules](#validation-rules) |
| `400` | `/pdf`, `/tex` | Job not in `done` state yet |
| `404` | `/v1/jobs/{id}`, `/pdf`, `/tex` | `job_id` does not exist |
| `500` | `/pdf`, `/tex` | File missing on disk despite `done` status |

### Job-level errors (`status: "failed"`, reason in `error`)

- The video URL could not be downloaded (404/403, network error, signed URL expired).
- A YouTube video was private, age-restricted, or region-blocked.
- The generated content did not compile as LaTeX (e.g. the system prompt did not
  instruct LaTeX output).

### Error response shape

```json
{ "detail": "Invalid API key" }
```

Validation (`422`) errors follow FastAPI's standard shape:

```json
{
  "detail": [
    {
      "loc": ["body", "system_prompt"],
      "msg": "String should have at least 1 character",
      "type": "string_too_short"
    }
  ]
}
```

---

## Writing a Good System Prompt

Because the output is compiled with XeLaTeX, your `system_prompt` should:

1. **Demand LaTeX body output only** — e.g. *"Output a LaTeX body only. Do not include
   `\documentclass`, `\usepackage`, or `\begin{document}`."*
2. **Keep math in LaTeX** — e.g. *"All math in LaTeX (`$...$` or `\begin{equation}`),
   never Unicode symbols."*
3. **Escape special characters** in text mode (`& % # _ $ { }`).
4. **Use only standard environments** that exist in a typical preamble (`itemize`,
   `enumerate`, `equation`, `align`, `figure`, `lstlisting`, etc.) — avoid inventing
   custom environments.

Your `user_prompt` is the *task* — what to produce from this particular video
(e.g. "recipe steps", "meeting minutes", "play-by-play timeline"). The transcript and
visual notes for each segment are appended to it automatically.

---

## Notes & Guarantees

- **Backward compatible.** The lecture endpoints and all existing request/response
  shapes, auth, status values, and download URLs are unchanged.
- **Same artifacts.** Custom jobs produce a PDF + `.tex` at the same
  `/v1/jobs/{id}/pdf` and `/v1/jobs/{id}/tex` endpoints as lecture jobs.
- **One video per job.** As with the other endpoints, each job processes exactly one
  video.
- **Prompt privacy.** `system_prompt` and `user_prompt` bodies are never written to
  server logs.
- **Hosted uploads.** Upload your video to storage first, then submit the resulting
  public or signed URL with `source.kind: "url"`. Make sure the URL is reachable by the
  server for the duration of the download.
```
