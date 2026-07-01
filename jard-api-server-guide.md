# Jard.ai Video Understanding API — Server Guide

> **Product:** Jard.ai — AI lecture & video understanding
> **Backend:** Qwen3.6-35B-A3B (vLLM) + Qwen3-ASR-1.7B
> **Output:** Structured LaTeX notes compiled to PDF (+ raw `.tex`)
> **Pattern:** Asynchronous job queue — submit → poll → download
> **Last updated:** 1 July 2026 — now with **output-language selection** (write notes in Arabic, Hebrew, English, and more).

---

## Table of Contents

1. [What's new — output language](#whats-new--output-language)
2. [Base URL](#base-url)
3. [Authentication](#authentication)
4. [Job lifecycle](#job-lifecycle)
5. [Submit endpoints](#submit-endpoints)
   - [URL](#1-summarize-by-url) · [YouTube](#2-summarize-by-youtube) · [Upload](#3-summarize-by-upload) · [Custom prompts](#4-summarize-custom-prompts)
6. [Output language — full reference](#output-language--full-reference)
7. [Poll & download endpoints](#poll--download-endpoints)
8. [Request & response schemas](#request--response-schemas)
9. [Limits](#limits)
10. [Error cases](#error-cases)
11. [End-to-end examples](#end-to-end-examples)

---

## What's new — output language

Every submit endpoint now accepts an optional **`output_language`** field: a short
language code that controls the language the finished notes are **written in**.

- **Optional.** Omit it and notes are written in **English** (previous behaviour, unchanged).
- **Short codes**, case-insensitive: `EN`, `AR`, `HE`, `FR`, `ES`, `ZH`, … (ISO 639-1).
- **Independent of `language`.** `language` is the *spoken/ASR* language of the video's
  audio; `output_language` is the language of the *written notes*. A Hebrew-spoken
  lecture can be turned into English notes, or an English lecture into Arabic notes —
  any combination.
- **The PDF genuinely renders the language.** The LaTeX engine (XeLaTeX) is configured
  per-job with the correct language, script font, and text direction. Right-to-left
  languages (**Arabic, Hebrew, Persian**) are typeset RTL with proper fonts (Amiri for
  Arabic, David CLM for Hebrew); section numbers, equation numbers, and theorem/
  definition labels are localized. Mathematics and code always stay left-to-right.
- **Never fails on a bad code.** An unknown or unsupported code silently falls back to
  English rather than erroring the job.

```jsonc
// Arabic notes from any video:
{ "video_url": "https://cdn.example.com/lecture.mp4", "output_language": "AR" }
```

See [Output language — full reference](#output-language--full-reference) for the code list
and behaviour details.

---

## Base URL

| Environment | Base URL |
|---|---|
| Production | `http://8.231.35.198:8080` |
| Self-hosted | `http://<your-host>:8080` |

Interactive OpenAPI docs: `http://8.231.35.198:8080/docs`

> ⚠️ **The production IP is ephemeral** — it changes whenever the VM is stopped/started.
> If you get connection errors or a 502, the IP has most likely rotated; ask Ameen for
> the current address (a static IP is planned). Everything below is relative to the base URL.

---

## Authentication

Every endpoint except `/health` requires an API key in the `X-API-Key` header. There is
**no** `Bearer` prefix and **no** `Authorization` header.

```
X-API-Key: 7db96b28bfafe87a851c22000e9758c5
```

| Status | Detail | Meaning |
|---|---|---|
| `401` | `Invalid API key` | Header missing or value incorrect |

> The default key above is a development key and must be rotated before any public
> deployment (`VIDEOARM_API_KEY` env var on the server).

---

## Job lifecycle

```
submit ──▶ [queued] ──▶ [downloading]* ──▶ [processing] ──┬─▶ [done]    (pdf_url + tex_url set)
                        (*URL/YouTube only)                └─▶ [failed]  (error set)
```

| Status | Meaning |
|---|---|
| `queued` | Accepted; waiting for a worker slot |
| `downloading` | Fetching the video (URL / YouTube jobs) |
| `processing` | Running the pipeline (ASR → vision → LaTeX → PDF) |
| `done` | PDF + LaTeX ready to download |
| `failed` | Pipeline error; see `error` |

Jobs are persisted in SQLite and **survive server restarts**: interrupted `processing`
jobs become `failed`, and `queued` jobs are automatically re-run. Up to **2 jobs**
process concurrently by default; the rest queue.

---

## Submit endpoints

All four return **`202 Accepted`** with a `JobStatus` you poll. All four accept the new
optional **`output_language`** field.

### 1. Summarize by URL

`POST /v1/summarize` · `application/json`

| Field | Type | Req | Description |
|---|---|:--:|---|
| `video_url` | string | ✅ | Direct/hosted/signed video URL (not YouTube — use endpoint 2 for that) |
| `title` | string | | Document title (default `"Lecture Notes"`) |
| `domain` | string | | Subject hint, e.g. `"Linear Algebra"` |
| `intent` | string | | Focus instruction, e.g. `"Emphasise proofs"` |
| `language` | string \| null | | ISO 639-1 **ASR** (spoken) language; `null` = auto-detect |
| `output_language` | string | | **NEW.** Language of the written notes (`"EN"`, `"AR"`, …). Default `EN` |

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url": "https://cdn.example.com/lecture.mp4",
    "title": "مقدمة في ميكانيكا الكم",
    "language": "en",
    "output_language": "AR"
  }'
```

### 2. Summarize by YouTube

`POST /v1/summarize/youtube` · `application/json`

Downloaded server-side with `yt-dlp` (≤720p). Same fields as endpoint 1, except the URL
field is `url`, and if `title` is omitted the video's own YouTube title is used.

| Field | Type | Req | Description |
|---|---|:--:|---|
| `url` | string | ✅ | YouTube video URL |
| `title` | string | | Defaults to the video's YouTube title |
| `domain` / `intent` | string | | As above |
| `language` | string \| null | | ASR language |
| `output_language` | string | | **NEW.** Written-notes language. Default `EN` |

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize/youtube \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{ "url": "https://youtu.be/jNQXAC9IVRw", "output_language": "HE" }'
```

### 3. Summarize by upload

`POST /v1/summarize/upload` · `multipart/form-data`

| Field | Type | Req | Description |
|---|---|:--:|---|
| `file` | file part | ✅ | The video file |
| `title` | string | | Default `"Lecture Notes"` |
| `domain` / `intent` | string | | As above |
| `language` | string | | ASR language |
| `output_language` | string | | **NEW.** Written-notes language. Default `EN` |

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize/upload \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -F "file=@/path/to/lecture.mp4" \
  -F "title=Quantum Mechanics" \
  -F "language=en" \
  -F "output_language=AR"
```

### 4. Summarize (custom prompts)

`POST /v1/summarize/custom` · `application/json`

Process any category of video with **your own** `system_prompt` + `user_prompt` instead
of the built-in lecture note-taker. Same pipeline, same PDF/`.tex` output.

| Field | Type | Req | Description |
|---|---|:--:|---|
| `source` | object | ✅ | `{ "kind": "url" \| "youtube", "url": "…" }`. `kind` optional — YouTube auto-detected from host |
| `system_prompt` | string | ✅ | Top-level instruction (1–20000 chars). Should tell the model to emit LaTeX body only |
| `user_prompt` | string | ✅ | Task instruction (1–20000 chars). Per-segment transcript + visual notes are appended automatically |
| `title` | string | | Default `"Notes"` |
| `category` | string | | Free-form label, stored for your bookkeeping; ignored by generation |
| `language` | string | | ASR language |
| `output_language` | string | | **NEW.** Written-notes language. Default `EN` |

> **Custom + `output_language`:** when you set a non-English `output_language`, a language
> directive is appended to your `system_prompt` so the notes come out in that language and
> the PDF is typeset for it. When `output_language` is `EN` (the default), your prompt is
> used verbatim and controls the language itself — so existing custom integrations are
> unchanged.

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize/custom \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "source": { "kind": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ" },
    "category": "meeting",
    "output_language": "HE",
    "system_prompt": "You summarise meetings into LaTeX minutes. Output LaTeX body only.",
    "user_prompt": "Produce minutes: attendees, decisions, and action items."
  }'
```

---

## Output language — full reference

### How to use it

Send `output_language` with any submit request. It is a short language code, **case-
insensitive**. Common names ("Arabic", "hebrew") and locale forms ("zh-CN", "fr-FR") are
also accepted. Omit it → **English**.

### Supported codes

| Direction | Codes |
|---|---|
| **Left-to-right (Latin/Cyrillic/Greek)** | `EN` English · `ES` Spanish · `FR` French · `DE` German · `IT` Italian · `PT` Portuguese · `NL` Dutch · `SV` Swedish · `PL` Polish · `CS` Czech · `TR` Turkish · `RU` Russian · `UK` Ukrainian · `EL` Greek |
| **Right-to-left (RTL)** | `AR` Arabic · `HE` Hebrew · `FA` Persian |
| **CJK (best-effort)** | `ZH` Chinese · `JA` Japanese · `KO` Korean |

Any code not in this list → **falls back to English** (the job still succeeds).

### What actually happens per language

- **RTL (`AR`, `HE`, `FA`)** — the whole document is typeset right-to-left with a proper
  script font (Arabic → *Amiri*, Hebrew → *David CLM*). Headings, page numbers, equation
  numbers, and theorem/definition/proof labels are localized. **Math and code stay
  left-to-right** and render normally inside RTL text.
- **Latin / Cyrillic / Greek** — standard LTR typesetting; the model writes the prose in
  that language, and built-in strings (e.g. "Contents") are localized.
- **CJK (`ZH`, `JA`, `KO`)** — rendered with Noto Sans CJK. Text and math render
  correctly; treat as best-effort (line-breaking is not as refined as a dedicated CJK
  engine).

### Verified rendering

Arabic, Hebrew, and CJK outputs have been compiled and visually verified end-to-end —
RTL layout, correct glyphs (no missing-character boxes), LTR math inside RTL prose, and
embedded Latin terms all render correctly.

---

## Poll & download endpoints

### List jobs — `GET /v1/jobs`
Returns `{ "jobs": [ JobStatus, … ] }`, newest first.

### Poll a job — `GET /v1/jobs/{job_id}`
Returns the current `JobStatus`. Poll every 8–15 s until `done` or `failed`.

### Download PDF — `GET /v1/jobs/{job_id}/pdf`
`application/pdf`. `400` if not `done`, `404` if unknown job, `500` if file missing.

### Download LaTeX — `GET /v1/jobs/{job_id}/tex`
`text/x-tex` — the raw source (closest thing to plain text). Same error codes as `/pdf`.

### Health — `GET /health`
No auth. `{ "status": "ok" }`.

```bash
curl http://8.231.35.198:8080/v1/jobs/cc99fe8101 \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5"

curl http://8.231.35.198:8080/v1/jobs/cc99fe8101/pdf \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" -o notes.pdf
```

---

## Request & response schemas

```typescript
interface SummarizeRequest {
  video_url: string;
  title?: string;                 // default "Lecture Notes"
  domain?: string;
  intent?: string;
  language?: string | null;       // ASR (spoken) language; null = auto-detect
  output_language?: string;       // NEW — written-notes language; default "en"
}

interface YouTubeRequest {
  url: string;
  title?: string;                 // default: the video's YouTube title
  domain?: string;
  intent?: string;
  language?: string | null;
  output_language?: string;       // NEW; default "en"
}

interface CustomRequest {
  source: { kind?: "url" | "youtube"; url: string };
  system_prompt: string;          // 1–20000 chars
  user_prompt: string;            // 1–20000 chars
  title?: string;                 // default "Notes"
  category?: string;
  language?: string | null;
  output_language?: string;       // NEW; default "en"
}

interface JobStatus {
  job_id: string;                 // 10-char hex
  status: "queued" | "downloading" | "processing" | "done" | "failed";
  pdf_url: string | null;         // set only when status === "done"
  tex_url: string | null;         // set only when status === "done"
  error: string | null;           // set only when status === "failed"
  segments_done: number;
  segments_total: number;
  estimated_seconds_remaining: number | null;  // null until 1st segment finishes
}
```

`estimated_seconds_remaining` is recomputed after each 5-minute segment as
`elapsed / segments_done × (segments_total − segments_done)`, so accuracy improves over time.

---

## Limits

| Limit | Value |
|---|---|
| Videos per job | 1 (multi-video planned for v2) |
| Concurrent jobs | 2 (server `VIDEOARM_WORKERS`) |
| URL/upload download timeout | 600 s |
| Custom prompt length | 1–20000 chars each (`VIDEOARM_MAX_PROMPT_CHARS`) |
| Video formats | MP4, MOV, AVI, MKV, WebM (anything OpenCV + ffmpeg reads) |
| Audio | Optional — silent videos use the visual-only pipeline |
| `output_language` | Optional; default `EN`; unknown → English |
| Output | PDF (XeLaTeX) + `.tex` source |

**Typical processing time:** roughly 6–12 min per 15 min of video; a 60-min lecture ≈
25–50 min depending on GPU load. Use `estimated_seconds_remaining` for a live estimate.

---

## Error cases

| Status | Endpoint | Meaning |
|---|---|---|
| `401` | all | Missing/invalid `X-API-Key` |
| `400` | `/pdf`, `/tex` | Job not `done` yet |
| `404` | `/v1/jobs/{id}`, `/pdf`, `/tex` | Unknown `job_id` |
| `422` | submit endpoints | Invalid body (bad URL; missing/over-length custom prompts) |
| `500` | `/pdf`, `/tex` | File missing on disk despite `done` |

> An invalid `output_language` does **not** cause a `422` — it falls back to English.

Job-level failures surface in the `JobStatus.error` field (not as HTTP errors), e.g.
`"yt-dlp did not produce a video file"`, `"No audio stream found in video"`,
`"Interrupted by server restart"`.

---

## End-to-end examples

### cURL — Arabic notes from an upload

```bash
#!/usr/bin/env bash
set -euo pipefail
KEY="7db96b28bfafe87a851c22000e9758c5"
BASE="http://8.231.35.198:8080"

JOB=$(curl -s -X POST "$BASE/v1/summarize/upload" \
  -H "X-API-Key: $KEY" \
  -F "file=@lecture.mp4" \
  -F "title=محاضرة في الجبر الخطي" \
  -F "language=en" \
  -F "output_language=AR" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "job=$JOB"

while :; do
  S=$(curl -s "$BASE/v1/jobs/$JOB" -H "X-API-Key: $KEY" \
       | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  echo "status=$S"; [[ "$S" == done || "$S" == failed ]] && break; sleep 10
done

[[ "$S" == done ]] && curl -s "$BASE/v1/jobs/$JOB/pdf" -H "X-API-Key: $KEY" -o notes_ar.pdf && echo "saved notes_ar.pdf"
```

### TypeScript — submit + poll + download

```typescript
const BASE = "http://8.231.35.198:8080";
const KEY  = process.env.JARD_API_KEY!;
const H    = { "X-API-Key": KEY, "Content-Type": "application/json" };

async function summarize(videoUrl: string, outputLanguage = "en") {
  const submit = await fetch(`${BASE}/v1/summarize`, {
    method: "POST", headers: H,
    body: JSON.stringify({ video_url: videoUrl, output_language: outputLanguage }),
  });
  if (submit.status !== 202) throw new Error(`submit ${submit.status}`);
  const { job_id } = await submit.json();

  // poll
  for (;;) {
    const job = await (await fetch(`${BASE}/v1/jobs/${job_id}`, { headers: { "X-API-Key": KEY } })).json();
    if (job.status === "done") {
      const pdf = await (await fetch(`${BASE}${job.pdf_url}`, { headers: { "X-API-Key": KEY } })).arrayBuffer();
      return Buffer.from(pdf);
    }
    if (job.status === "failed") throw new Error(`job failed: ${job.error}`);
    await new Promise(r => setTimeout(r, 8000));
  }
}

// Hebrew notes:
await summarize("https://cdn.example.com/lecture.mp4", "HE");
```

---

*Questions or a new language you need supported? Ping Ameen.*
