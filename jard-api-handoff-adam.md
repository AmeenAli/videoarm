# Jard.ai — API Handoff for Adam

> Everything you need to build your app on top of the Jard.ai video‑understanding API:
> new server address, the API key, every endpoint, and copy‑paste examples.
>
> **Owner:** Ameen — ping me if anything below 502s or the address changes.
> **Last updated:** 29 July 2026.

---

## 0. TL;DR — what changed

- **The server has a stable address now.** Base URL is **`http://spark.jard.ai:8080`** — a domain
  backed by a **static IP**, so it survives VM restarts. Any hard‑coded IPs from older docs
  (`8.231.35.198` / `35.224.182.54` / `34.71.177.8` / `35.253.209.85`) are **stale** — update them.
- The API key, endpoints, request/response shapes, and behaviour are **unchanged**.
  If you already integrated against an old address, the *only* thing you touch is the base URL.
- Still keep the base URL in one config value, not sprinkled through the code. Note it's plain
  HTTP (no TLS yet), so call it from your backend only.

---

## 1. Base URL & auth

| | |
|---|---|
| **Base URL** | `http://spark.jard.ai:8080` |
| **Interactive API docs** | `http://spark.jard.ai:8080/docs` (Swagger UI — try every endpoint from the browser) |
| **Health check** | `GET http://spark.jard.ai:8080/health` → `{"status":"ok"}` (no auth) |
| **Auth header** | `X-API-Key: 7db96b28bfafe87a851c22000e9758c5` |

- The header is **exactly** `X-API-Key`. There is **no** `Bearer` prefix and **no** `Authorization` header.
- Every endpoint except `/health` requires it. Missing/wrong key → `401 Invalid API key`.
- This is a **dev key** — I'll rotate it before any real launch, so keep it in an env var, not inline.

> **Call the API from your backend, not the browser.** The key would be exposed in dev‑tools,
> and the server sends no CORS headers. Shape it as: your frontend → your backend → Jard API.

---

## 2. How the API works (the whole model in 20 seconds)

It's an **asynchronous job queue**. You never hold a request open while a video processes.

```
POST /v1/summarize/...   →  202 { job_id, status:"queued" }
GET  /v1/jobs/{job_id}   →  poll every 8–15 s until status == "done" | "failed"
GET  /v1/jobs/{job_id}/pdf   →  the finished PDF
GET  /v1/jobs/{job_id}/tex   →  the raw LaTeX source (closest thing to plain text)
```

Job status flow:

```
queued → downloading* → processing → done      (pdf_url + tex_url set)
                                    ↘ failed    (error set)
   (*URL / YouTube jobs only)
```

| Status | Meaning |
|---|---|
| `queued` | Accepted, waiting for a worker |
| `downloading` | Fetching the video (URL/YouTube jobs) |
| `processing` | Running the pipeline (ASR → vision → LaTeX → PDF) |
| `done` | PDF + `.tex` ready |
| `failed` | See the `error` field |

Jobs are stored in SQLite and **survive server restarts** — persist the `job_id` and you can resume
polling even after the user closes the tab. Up to **2 jobs** process concurrently; the rest queue.

The output is always a **PDF + a `.tex` source**, both compiled from structured LaTeX notes.

---

## 3. Submit endpoints

There are **6** ways to submit. All return **`202`** with a `{ job_id, status }` you poll.
All accept an optional **`output_language`** (see §5).

### 3.1 By direct URL — `POST /v1/summarize`  ·  `application/json`

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `video_url` | string | ✅ | Direct/hosted/signed video URL (**not** YouTube — use 3.2) |
| `title` | string | | Default `"Lecture Notes"` |
| `domain` | string | | Subject hint, e.g. `"Linear Algebra"` |
| `intent` | string | | Focus instruction, e.g. `"Emphasise proofs"` |
| `language` | string \| null | | **Spoken** (ASR) language, ISO‑639‑1; `null` = auto‑detect |
| `output_language` | string | | Language the **notes are written in** (see §5). Default `EN` |

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{ "video_url": "https://cdn.example.com/lecture.mp4", "title": "Week 1", "language": "en" }'
```

### 3.2 By YouTube — `POST /v1/summarize/youtube`  ·  `application/json`

Downloaded server‑side with `yt-dlp` (≤720p). Same fields as 3.1 **except** the URL field is `url`,
and if you omit `title` the video's own YouTube title is used.

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize/youtube \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{ "url": "https://youtu.be/jNQXAC9IVRw" }'
```

> YouTube sometimes bot‑blocks a datacenter IP. The server is configured with cookies to work around
> it, but a direct file URL is always the most reliable. If a specific link fails, retry later or use a URL.

### 3.3 By upload — `POST /v1/summarize/upload`  ·  `multipart/form-data`

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `file` | file part | ✅ | The video file |
| `title` / `domain` / `intent` / `language` / `output_language` | | | As above (form fields) |

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize/upload \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -F "file=@/path/to/lecture.mp4" \
  -F "title=Quantum Mechanics" \
  -F "language=en"
```

### 3.4 Custom prompts — `POST /v1/summarize/custom`  ·  `application/json`

Process **any** category of video with your own prompts instead of the built‑in lecture note‑taker.
Same pipeline, same PDF/`.tex` output.

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `source` | object | ✅ | `{ "kind": "url" \| "youtube", "url": "…" }`. `kind` optional — YouTube auto‑detected from host |
| `system_prompt` | string | ✅ | Top‑level instruction (1–20000 chars). Tell the model to emit **LaTeX body only** |
| `user_prompt` | string | ✅ | Per‑task instruction (1–20000 chars). Per‑segment transcript + visual notes are appended automatically |
| `title` | string | | Default `"Notes"` |
| `category` | string | | Free‑form label, stored for your bookkeeping; ignored by generation |
| `language` / `output_language` | | | As above |

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize/custom \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "source": { "kind": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ" },
    "category": "meeting",
    "system_prompt": "You summarise meetings into LaTeX minutes. Output LaTeX body only.",
    "user_prompt": "Produce minutes: attendees, decisions, and action items."
  }'
```

### 3.5 Multi‑video (stack) — `POST /v1/summarize/multi`  ·  `application/json`

Submit an **ordered list of videos as ONE job** → get back **ONE combined document** (single PDF +
single `.tex`): shared title page, one table of contents, each video becomes a numbered `\section`
in submission order. Direct URLs and YouTube links can be mixed in one stack.

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `videos` | array | ✅ | **Ordered** list, 1–8 entries |
| `videos[].url` | string | ✅ | Direct URL or YouTube link |
| `videos[].kind` | `"url"`\|`"youtube"` | | Optional — auto‑detected; explicit wins |
| `videos[].title` | string | | Section heading for that video |
| `title` | string | | Title of the **combined** doc (default `"Combined Notes"`) |
| `domain` / `intent` / `language` / `output_language` | | | Applied to the **whole** stack |

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize/multi \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "ML Course — Lectures 1–2",
    "videos": [
      { "url": "https://cdn.example.com/ml-lec1.mp4", "title": "Lecture 1: Regression" },
      { "url": "https://youtu.be/abc123xyz",          "title": "Lecture 2: Classification" }
    ]
  }'
```

### 3.6 Multi‑video upload — `POST /v1/summarize/multi/upload`  ·  `multipart/form-data`

Same as 3.5 but with **repeated `files` parts** (order = section order; each filename minus extension
becomes its section title). Mixing local files and URLs in one stack is **not** supported — pick one
endpoint per job.

```bash
curl -X POST http://spark.jard.ai:8080/v1/summarize/multi/upload \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -F "files=@part1.mp4" -F "files=@part2.mp4" -F "files=@part3.mp4" \
  -F "title=Databases — Full Lecture"
```

> In fetch/axios, append the same field name repeatedly: `form.append("files", f1); form.append("files", f2)`.
> Do **not** set `Content-Type` yourself on multipart — let the client set the boundary.

---

## 4. Poll & download

| Call | Returns |
|---|---|
| `GET /v1/jobs` | `{ "jobs": [JobStatus, …] }`, newest first |
| `GET /v1/jobs/{job_id}` | The current `JobStatus`. Poll every 8–15 s |
| `GET /v1/jobs/{job_id}/pdf` | `application/pdf` (the combined PDF for multi jobs) |
| `GET /v1/jobs/{job_id}/tex` | `text/x-tex` — the raw source |

`pdf_url` / `tex_url` in the job status are **relative paths** — prefix the base URL and send the key.

```typescript
interface JobStatus {
  job_id: string;                              // 10‑char hex
  status: "queued"|"downloading"|"processing"|"done"|"failed";
  pdf_url: string | null;                      // set only when done
  tex_url: string | null;                      // set only when done
  error:   string | null;                      // set only when failed
  segments_done:  number;
  segments_total: number;                      // for multi jobs: aggregated over ALL videos
  estimated_seconds_remaining: number | null;  // null until 1st segment finishes
}
```

`estimated_seconds_remaining` recomputes after each 5‑minute segment, so it gets more accurate over time.
Use it to drive a "~N min remaining" label; show a spinner while it's `null`.

---

## 5. `output_language` (optional, all endpoints)

Controls the language the finished **notes are written in** — independent of `language` (which is the
*spoken/ASR* language of the audio). A Hebrew‑spoken lecture → English notes, or vice‑versa.

- Omit it → **English** (unchanged default).
- Short codes, case‑insensitive: `EN ES FR DE IT PT NL SV PL CS TR RU UK EL` (LTR),
  `AR HE FA` (RTL, typeset right‑to‑left with proper fonts — Amiri / David CLM),
  `ZH JA KO` (CJK, best‑effort). Common names ("Arabic") and locale forms ("zh-CN") also accepted.
- **Never errors on a bad code** — an unknown code silently falls back to English (the job still succeeds).
- Math and code always stay left‑to‑right, even inside RTL prose.

```jsonc
{ "video_url": "https://cdn.example.com/lecture.mp4", "output_language": "AR" }  // Arabic notes
```

---

## 6. Limits

| Limit | Value |
|---|---|
| Videos per single job | 1 |
| Videos per multi job | 1–8 |
| Concurrent jobs | 2 (rest queue) |
| Custom prompt length | 1–20000 chars each |
| URL / upload download timeout | 600 s per video |
| Video formats | MP4, MOV, AVI, MKV, WebM (anything OpenCV + ffmpeg reads) |
| Audio | Optional — silent videos use the visual‑only pipeline |
| Output | PDF (XeLaTeX) + `.tex` |

**Rough processing time:** ~6–12 min per 15 min of video. A 60‑min lecture ≈ 25–50 min depending on
GPU load. A multi stack ≈ the sum of its parts. Design for "submit and come back", not a blocking spinner.

---

## 7. Error cases

| Status | Where | Meaning |
|---|---|---|
| `401` | all | Missing/invalid `X-API-Key` |
| `400` | `/pdf`, `/tex` | Job not `done` yet |
| `404` | `/v1/jobs/{id}`, `/pdf`, `/tex` | Unknown `job_id` |
| `422` | submit endpoints | Bad body (invalid URL, empty/oversized prompts, empty or >8 `videos`) |
| `500` | `/pdf`, `/tex` | File missing on disk despite `done` |

Job‑level failures surface in `JobStatus.error` (not as an HTTP error), e.g.
`"No audio stream found in video"`, `"yt-dlp did not produce a video file"`,
`"Interrupted by server restart"`. For **multi** jobs it's all‑or‑nothing — one bad video fails the
whole job and `error` names which one (`"Video 2/3 (…): …"`); fix that one and resubmit.

**Two sharp edges to handle in the app:**
- **Connection refused / 502** → the server is likely down or restarting. Surface "service
  unavailable" and ping me; don't retry‑loop forever.
- **401** → key misconfigured or rotated server‑side.

---

## 8. Minimal end‑to‑end client (TypeScript)

```typescript
const BASE = process.env.JARD_BASE_URL!;   // "http://spark.jard.ai:8080" — keep as config value
const KEY  = process.env.JARD_API_KEY!;    // header name is exactly "X-API-Key"

async function summarizeUrl(videoUrl: string, outputLanguage = "en") {
  const submit = await fetch(`${BASE}/v1/summarize`, {
    method: "POST",
    headers: { "X-API-Key": KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ video_url: videoUrl, output_language: outputLanguage }),
  });
  if (submit.status !== 202) throw new Error(`submit ${submit.status}: ${await submit.text()}`);
  const { job_id } = await submit.json();     // persist this — jobs survive restarts

  for (;;) {
    const job = await (await fetch(`${BASE}/v1/jobs/${job_id}`,
      { headers: { "X-API-Key": KEY } })).json();
    if (job.status === "done") {
      const pdf = await (await fetch(`${BASE}${job.pdf_url}`,
        { headers: { "X-API-Key": KEY } })).arrayBuffer();
      return Buffer.from(pdf);                 // stream to the user from YOUR backend
    }
    if (job.status === "failed") throw new Error(`job failed: ${job.error}`);
    await new Promise(r => setTimeout(r, 10_000));
  }
}
```

For an in‑app **text** view instead of the PDF, fetch `tex_url` and strip the LaTeX (see
`api-output-guide.md` in the repo for a ready‑made `stripLatex()`).

---

## 9. Quick smoke test (paste into a terminal)

```bash
BASE="http://spark.jard.ai:8080"
KEY="7db96b28bfafe87a851c22000e9758c5"

curl -s "$BASE/health"                          # → {"status":"ok"}
curl -s "$BASE/v1/jobs" -H "X-API-Key: $KEY"    # → {"jobs":[...]}  (401 means bad key)
```

If `/health` doesn't answer, the server is down or restarting — ping me.

---

*Questions, or a language/feature you need? — Ameen.*
