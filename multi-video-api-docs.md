# Jard.ai — Multi-Video (Stack) Endpoint Reference

> **Feature:** Submit a **stack of videos as ONE job** → get back **ONE combined
> document** (single PDF + single `.tex`).
> **Endpoints:** `POST /v1/summarize/multi` (URLs / YouTube) · `POST /v1/summarize/multi/upload` (file uploads)
> **Pattern:** Same asynchronous job queue as everything else — submit → poll → download.
> **Status:** ✅ **DEPLOYED & LIVE** on production (`http://8.231.35.198:8080`) as of 10 July 2026 —
> verified end-to-end (2-part upload → one combined PDF). Also visible in the
> interactive docs at `http://8.231.35.198:8080/docs`.
> **Last updated:** 10 July 2026

The existing single-video endpoints (`/v1/summarize`, `/v1/summarize/youtube`,
`/v1/summarize/upload`, `/v1/summarize/custom`) are **completely unchanged** — this
is a purely additive route with its own processing service in the backend. No
client migration is required.

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Base URL & Authentication](#base-url--authentication)
3. [Submit — URLs / YouTube](#1-submit--urls--youtube)
4. [Submit — multiple file uploads](#2-submit--multiple-file-uploads)
5. [What the combined document looks like](#what-the-combined-document-looks-like)
6. [Polling & progress semantics](#polling--progress-semantics)
7. [Downloading the result](#downloading-the-result)
8. [Limits](#limits)
9. [Error cases](#error-cases)
10. [Building your app on top of this](#building-your-app-on-top-of-this)
11. [End-to-end examples](#end-to-end-examples)
12. [Notes & guarantees](#notes--guarantees)

---

## What it does

You send an **ordered list of videos** — e.g. the three parts of a lecture series,
or one lecture that was recorded in several chunks. The backend:

1. Downloads/receives every video in the stack (any one failing fails the whole
   job — you never get a document with silently missing parts).
2. Runs each video through the **exact same understanding pipeline** as the
   single-video endpoints (per-segment ASR → visual frame extraction → figure
   selection → LaTeX synthesis).
3. Stitches the results into **one document, in the order you submitted**:
   each video becomes a numbered `\section` (its title = your per-video title,
   the YouTube title, or the uploaded filename).
4. Compiles **once** → one PDF + one `.tex`, downloadable from the standard
   `/v1/jobs/{id}/pdf` and `/v1/jobs/{id}/tex` endpoints.

So yes — the stack is processed "as a whole": shared title page, one table of
contents covering all parts, continuous theorem/equation numbering per section,
and figures from every video in one document.

`output_language` works exactly as on the other endpoints (Arabic/Hebrew RTL
PDFs etc.) and applies to the whole document.

---

## Base URL & Authentication

Same as the rest of the API (see the main server guide):

| Environment | Base URL |
|---|---|
| Production | `http://8.231.35.198:8080` |
| Self-hosted | `http://<your-host>:8080` |

> ⚠️ The production IP is ephemeral — it rotates when the VM restarts. If you get
> connection errors, ask Ameen for the current address.

Every request needs the API key header — exactly `X-API-Key`, no `Bearer`:

```
X-API-Key: 7db96b28bfafe87a851c22000e9758c5
```

---

## 1. Submit — URLs / YouTube

`POST /v1/summarize/multi` · `application/json` · returns **202**

Direct video URLs and YouTube links can be **freely mixed in one stack**.

### Body

| Field | Type | Req | Description |
|---|---|:--:|---|
| `videos` | array | ✅ | **Ordered** list of sources, 1–8 entries (order = section order) |
| `videos[].url` | string | ✅ | Direct/hosted/signed video URL **or** YouTube link |
| `videos[].kind` | `"url"` \| `"youtube"` | | Optional — YouTube is auto-detected from the host; an explicit `kind` wins |
| `videos[].title` | string | | Section heading for this video. Default: the YouTube title (YouTube sources) or `"Video i"` |
| `title` | string | | Title of the **combined** document (default `"Combined Notes"`) |
| `domain` | string | | Subject hint applied to every video, e.g. `"Linear Algebra"` |
| `intent` | string | | Focus instruction applied to every video |
| `language` | string \| null | | Spoken/ASR language for **all** videos; `null` = auto-detect |
| `output_language` | string | | Written-notes language for the whole document (`"EN"`, `"AR"`, `"HE"`, …). Default `EN`; unknown codes fall back to English |

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize/multi \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Quantum Mechanics — Weeks 1–3",
    "videos": [
      { "url": "https://cdn.example.com/qm-week1.mp4", "title": "Week 1: State Vectors" },
      { "url": "https://youtu.be/abc123xyz",           "title": "Week 2: Operators" },
      { "url": "https://cdn.example.com/qm-week3.mp4" }
    ],
    "language": "en",
    "output_language": "AR"
  }'
```

Response (`202`):

```json
{ "job_id": "f3a91c02d7", "status": "queued" }
```

---

## 2. Submit — multiple file uploads

`POST /v1/summarize/multi/upload` · `multipart/form-data` · returns **202**

Attach **several `files` parts in one request** — this is the "stack of files"
upload. Part order = section order; each file's name (minus extension) becomes
its section title.

| Field | Type | Req | Description |
|---|---|:--:|---|
| `files` | file part, **repeated** | ✅ | 1–8 video files, in order |
| `title` | string | | Combined document title (default `"Combined Notes"`) |
| `domain` / `intent` | string | | As above, applied to every video |
| `language` | string | | Spoken/ASR language for all videos |
| `output_language` | string | | Written-notes language. Default `EN` |

```bash
curl -X POST http://8.231.35.198:8080/v1/summarize/multi/upload \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -F "files=@part1.mp4" \
  -F "files=@part2.mp4" \
  -F "files=@part3.mp4" \
  -F "title=Databases — Full Lecture" \
  -F "output_language=HE"
```

> In fetch/axios: append the same field name repeatedly —
> `form.append("files", file1); form.append("files", file2); …`

---

## What the combined document looks like

```
Title page          ← `title` of the request
Table of contents   ← lists every section (= every video) and its subsections
\section{Week 1: State Vectors}     ← video 1 (your title / YouTube title / filename)
    \subsection{...}  ...notes...   ← the normal per-segment lecture notes
\section{Week 2: Operators}         ← video 2, starts on a fresh page
    ...
```

- Videos appear **exactly in submission order**.
- Theorem / definition / equation numbering is per-section (`1.1`, `2.3`, …), so
  each video's numbering is clean.
- Figures (board/slide screenshots) from all videos are included and namespaced
  internally — no collisions.
- RTL languages (`AR`, `HE`, `FA`) typeset the whole combined document
  right-to-left, exactly like the single-video endpoints.

---

## Polling & progress semantics

Poll the standard endpoint — nothing new to integrate:

```
GET /v1/jobs/{job_id}
```

Statuses are the usual `queued → downloading → processing → done | failed`.
Two things to know for multi jobs:

- **`downloading` covers the whole stack** — all sources are fetched before
  processing starts.
- **`segments_done` / `segments_total` are aggregated across all videos.**
  The total is known as soon as processing starts (every file is probed up
  front), so `estimated_seconds_remaining` is meaningful for the entire job,
  not just the current video. Videos are processed sequentially, so expect
  roughly the sum of the individual videos' processing times.

Poll every 8–15 s, same as the other endpoints.

---

## Downloading the result

Identical to every other job type:

| Endpoint | Content |
|---|---|
| `GET /v1/jobs/{job_id}/pdf` | The **one combined** PDF |
| `GET /v1/jobs/{job_id}/tex` | The **one combined** LaTeX source |

`400` if not `done` yet, `404` unknown job, `500` file missing on disk.

---

## Limits

| Limit | Value |
|---|---|
| Videos per multi job | **1–8** (server `VIDEOARM_MAX_MULTI_VIDEOS`) |
| Mixing URL + YouTube in one stack | ✅ allowed (JSON endpoint) |
| Mixing uploads with URLs in one stack | ❌ — use one endpoint or the other per job |
| Per-video download timeout | 600 s (direct URLs) |
| `language` / `output_language` | Job-wide (apply to every video) |
| Output | One PDF + one `.tex` for the whole stack |

**Processing time:** videos run one after another (each video is internally
parallelised on the GPU), so budget roughly the sum of what each video would
take alone — e.g. three 15-minute parts ≈ 3 × (6–12 min).

---

## Error cases

| Status | When |
|---|---|
| `401` | Missing/invalid `X-API-Key` |
| `422` | Empty `videos` array, more than the max, or an invalid URL |
| `202` + later `failed` | Any download/processing failure — see `error` in the job status |

**All-or-nothing:** if any single video in the stack fails (unreachable URL,
YouTube block, corrupt file), the **whole job fails** and `error` tells you which
one, e.g.:

```json
{ "status": "failed", "error": "Video 2/3 (https://youtu.be/abc123xyz): ..." }
```

Fix or drop that video and resubmit. This is deliberate — a "successful" document
with silently missing parts would be worse.

Jobs survive server restarts like all others: a queued multi job is re-run
automatically (URL/YouTube sources are re-downloaded); a job interrupted
mid-download or mid-processing becomes `failed` with `"Interrupted by server restart"`.

---

## Building your app on top of this

The complete client-side flow — this is everything your app needs to do.
(All snippets are plain `fetch`; nothing framework-specific.)

### 0. Config

```typescript
const BASE = "http://8.231.35.198:8080";     // ⚠️ IP rotates on VM restart — make it a config value, not a constant
const KEY  = process.env.JARD_API_KEY!;      // header name is exactly "X-API-Key"
```

> **Call the API from your backend, not the browser.** The key would be visible
> in browser dev tools, and the server does not send CORS headers. Typical shape:
> your frontend → your backend → Jard API.

### 1. Let the user build a stack

Your UI collects an **ordered** list of videos — a multi-file picker
(drag-to-reorder is nice, order = section order in the PDF) and/or URL/YouTube
inputs. Per video, optionally let the user name it (that name becomes the
section heading). Enforce **max 8** client-side so users get instant feedback
instead of a `422`.

- All uploads → `POST /v1/summarize/multi/upload`
- All URLs/YouTube (mixable) → `POST /v1/summarize/multi`
- A mix of local files and URLs in one stack is not supported — if your UI
  allows both at once, upload the URLs' files yourself or split the UX.

### 2. Submit → store the job_id

```typescript
async function submitStack(files: File[], title: string, outputLanguage = "en") {
  const form = new FormData();
  for (const f of files) form.append("files", f);        // same name, repeated — order matters
  form.append("title", title);
  form.append("output_language", outputLanguage);
  const res = await fetch(`${BASE}/v1/summarize/multi/upload`, {
    method: "POST", headers: { "X-API-Key": KEY }, body: form,
  });
  if (res.status !== 202) throw new Error(`submit failed: ${res.status} ${await res.text()}`);
  return (await res.json()).job_id as string;            // persist this — it survives everything
}
```

Persist `job_id` (DB row / localStorage). Jobs survive server restarts, so your
app can resume polling after the user closes the tab — you never need to
resubmit just because the client went away.

### 3. Poll → drive your UI

Poll `GET /v1/jobs/{job_id}` every **10 s**. Map the response straight onto UI
state:

| API field | UI |
|---|---|
| `status: "queued"` | "Waiting in queue…" |
| `status: "downloading"` | "Fetching videos…" (covers the whole stack) |
| `status: "processing"` | Progress bar: `segments_done / segments_total` (aggregated over ALL videos) |
| `estimated_seconds_remaining` | "~N min remaining" (null until the 1st segment finishes — show a spinner till then) |
| `status: "done"` | Show download / view buttons (`pdf_url`, `tex_url` are set) |
| `status: "failed"` | Show `error` verbatim — it names the offending video ("Video 2/3 (…): …") so the user knows what to fix |

### 4. Fetch the result

`pdf_url` / `tex_url` are **relative paths** — prefix `BASE` and send the key:

```typescript
const pdf = await fetch(`${BASE}${job.pdf_url}`, { headers: { "X-API-Key": KEY } });
// stream it to the user, or proxy it through your backend
```

Serve the PDF from your backend (don't hand the browser a URL containing the
key). For an in-app text view, fetch `tex_url` and strip the LaTeX — see
`api-output-guide.md` for a ready-made `stripLatex()` and the
LLM-post-processing option.

### 5. Handle the sharp edges

- **Connection refused / 502** → the VM IP rotated; surface "service address
  changed" and ping Ameen. Don't retry-loop forever.
- **401** → key misconfigured (or rotated server-side).
- **`failed` with a YouTube error** → usually YouTube bot-blocking; retrying the
  same link later sometimes works, a direct file URL always works.
- **Long stacks are long jobs** — 3 × 15-min videos ≈ 20–35 min total. Design
  for "submit and come back", not a blocking spinner: job list screen +
  polling, exactly like the single-video flow.
- Uploads: no per-file size cap is enforced, but send reasonable encodes
  (≤720p is plenty — the pipeline never samples more).

That's the whole integration — if you already built against the single-video
endpoints, only step 1–2 are new; polling and download are byte-identical.

---

## End-to-end examples

### cURL — three uploads → one Hebrew PDF

```bash
#!/usr/bin/env bash
set -euo pipefail
KEY="7db96b28bfafe87a851c22000e9758c5"
BASE="http://8.231.35.198:8080"

JOB=$(curl -s -X POST "$BASE/v1/summarize/multi/upload" \
  -H "X-API-Key: $KEY" \
  -F "files=@part1.mp4" -F "files=@part2.mp4" -F "files=@part3.mp4" \
  -F "title=אלגברה לינארית — הרצאה מלאה" \
  -F "output_language=HE" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "job=$JOB"

while :; do
  J=$(curl -s "$BASE/v1/jobs/$JOB" -H "X-API-Key: $KEY")
  S=$(echo "$J" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  echo "status=$S  ($(echo "$J" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['segments_done'],'/',d['segments_total'])"))"
  [[ "$S" == done || "$S" == failed ]] && break; sleep 10
done

[[ "$S" == done ]] && curl -s "$BASE/v1/jobs/$JOB/pdf" -H "X-API-Key: $KEY" -o combined.pdf && echo "saved combined.pdf"
```

### TypeScript — mixed URL + YouTube stack

```typescript
const BASE = "http://8.231.35.198:8080";
const KEY  = process.env.JARD_API_KEY!;

async function summarizeStack() {
  const submit = await fetch(`${BASE}/v1/summarize/multi`, {
    method: "POST",
    headers: { "X-API-Key": KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      title: "ML Course — Lectures 1–2",
      videos: [
        { url: "https://cdn.example.com/ml-lec1.mp4", title: "Lecture 1: Regression" },
        { url: "https://youtu.be/abc123xyz",          title: "Lecture 2: Classification" },
      ],
      output_language: "en",
    }),
  });
  if (submit.status !== 202) throw new Error(`submit ${submit.status}`);
  const { job_id } = await submit.json();

  for (;;) {
    const job = await (await fetch(`${BASE}/v1/jobs/${job_id}`,
      { headers: { "X-API-Key": KEY } })).json();
    console.log(`${job.status} ${job.segments_done}/${job.segments_total}`,
      job.estimated_seconds_remaining != null ? `~${job.estimated_seconds_remaining}s left` : "");
    if (job.status === "done")
      return Buffer.from(await (await fetch(`${BASE}${job.pdf_url}`,
        { headers: { "X-API-Key": KEY } })).arrayBuffer());
    if (job.status === "failed") throw new Error(job.error);
    await new Promise(r => setTimeout(r, 10_000));
  }
}
```

### Multi-file upload from the browser / Node

```typescript
const form = new FormData();
form.append("files", filePart1);   // same field name, repeated — order matters
form.append("files", filePart2);
form.append("files", filePart3);
form.append("title", "Physics 101 — Full Day");
form.append("output_language", "AR");

const res = await fetch(`${BASE}/v1/summarize/multi/upload`, {
  method: "POST",
  headers: { "X-API-Key": KEY },   // do NOT set Content-Type — the browser sets the multipart boundary
  body: form,
});
const { job_id } = await res.json();  // then poll as usual
```

---

## Notes & guarantees

- **Isolation:** multi-video jobs run through a dedicated backend service
  (`MultiVideoSummarizer`); the single-video endpoints, their behaviour, and
  their performance are untouched.
- **One job, one bill, one artifact:** a stack is one `job_id`, one row in
  `/v1/jobs`, one PDF, one `.tex`.
- **Order is sacred:** sections appear exactly in the order you submitted the
  videos / attached the file parts.
- **Job-wide settings:** `language`, `output_language`, `domain`, `intent` apply
  to every video in the stack. If two videos need different spoken languages,
  leave `language` unset (auto-detect handles per-video audio).
- A single-entry stack is allowed and behaves like a single-video job with a
  section heading — handy if you want one code path client-side.

*Questions? Ping Ameen.*
