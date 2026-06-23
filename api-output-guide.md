# Jard.ai — API Output Guide

## What the API Returns

The API does **not** return plain text directly. It returns a compiled **PDF** and the raw **LaTeX source** (`.tex`) that generated it.

---

## Output Endpoints

Once a job reaches `status: "done"`, two download endpoints become available:

| Endpoint | Content-Type | Description |
|---|---|---|
| `GET /v1/jobs/{job_id}/pdf` | `application/pdf` | Compiled lecture notes PDF |
| `GET /v1/jobs/{job_id}/tex` | `text/x-tex` | Raw LaTeX source (closest thing to plain text) |

Both require the `X-API-Key` header. These endpoints are identical for **all** job types — default lecture jobs (`/v1/summarize`, `/v1/summarize/youtube`, `/v1/summarize/upload`) and custom prompt-configurable jobs (`/v1/summarize/custom`) all produce a PDF + `.tex` retrievable here.

---

## Full Job Status Response (when done)

```json
{
  "job_id": "cc99fe8101",
  "status": "done",
  "pdf_url": "/v1/jobs/cc99fe8101/pdf",
  "tex_url": "/v1/jobs/cc99fe8101/tex",
  "error": null,
  "segments_done": 12,
  "segments_total": 12,
  "estimated_seconds_remaining": null
}
```

---

## Downloading the LaTeX Source (Text)

```bash
curl http://35.224.182.54:8080/v1/jobs/cc99fe8101/tex \
  -H "X-API-Key: 7db96b28bfafe87a851c22000e9758c5" \
  -o lecture_notes.tex
```

The `.tex` file contains the full structured notes in LaTeX. If you need plain text (e.g., for display in-app or feeding back into an LLM), you have two options:

**Option A — Strip LaTeX tags in JavaScript:**

```typescript
function stripLatex(tex: string): string {
  return tex
    .replace(/\\[a-zA-Z]+(\{[^}]*\})?/g, "$1") // replace \cmd{x} with x
    .replace(/[{}]/g, "")                        // remove remaining braces
    .replace(/\n{3,}/g, "\n\n")                  // collapse blank lines
    .trim();
}

const res = await fetch(`${BASE}/v1/jobs/${jobId}/tex`, {
  headers: { "X-API-Key": API_KEY },
});
const tex = await res.text();
const plainText = stripLatex(tex);
```

**Option B — Request a `/text` endpoint** (not yet implemented — ask Ameen to add it).

---

## If You Need the PDF as Text (Node.js)

Use a PDF-to-text library like `pdf-parse`:

```typescript
import pdfParse from "pdf-parse";

const res = await fetch(`${BASE}/v1/jobs/${jobId}/pdf`, {
  headers: { "X-API-Key": API_KEY },
});
const pdfBuffer = Buffer.from(await res.arrayBuffer());
const { text } = await pdfParse(pdfBuffer);
console.log(text);
```

---

## Summary

| Need | Use |
|---|---|
| Display/share notes | Download PDF (`/pdf`) |
| Parse structure or feed into LLM | Download LaTeX (`/tex`) and strip tags, or use `pdf-parse` on the PDF |
| Raw plain text endpoint | Not yet available — can be added on request |
