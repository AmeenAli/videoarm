# Implementation Plan — `POST /v1/summarize/multi/custom`

> **Status:** PLAN ONLY — not implemented yet.
> **Goal:** custom-prompt processing (à la `/v1/summarize/custom`) over an ordered stack
> of 1–8 videos (à la `/v1/summarize/multi`) → one combined PDF + `.tex`.
> **Approach:** pure composition of existing infrastructure. No new services, no schema
> migration, no changes to any existing endpoint's behaviour.

---

## 0. Why this is small

The heavy lifting already exists and already composes:

- `MultiVideoSummarizer.summarize()` (`videoarm/core/multi_summarizer.py`) **already
  accepts `system_prompt` / `user_prompt`** and forwards them to
  `LectureSummarizer.summarize_body()` per video — the parameters are simply never fed
  from any route today.
- `LectureSummarizer.summarize_body()` already implements custom-mode semantics
  (system prompt replaces note-taker prompt; user prompt replaces the synthesis
  template with `_CUSTOM_DATA_BLOCK` appended per segment).
- The `jobs` table already has `system_prompt`, `user_prompt`, `category`, and
  `sources_json` columns — **no DB migration needed**.
- `_run_multi_job` / `resume_multi_job` already implement download, all-or-nothing
  failure with `"Video i/N (…): …"`, aggregated progress, cleanup, and restart recovery.

The entire change is: **one new route + threading two parameters through two functions.**

---

## 1. API contract, schemas, OpenAPI

### Route

`POST /v1/summarize/multi/custom` · `application/json` · returns `202 JobAccepted`
· auth via existing `_require_key` dependency in `videoarm/api/multi.py`.

Lives in `videoarm/api/multi.py` next to the other two multi routes (keeps the
"multi is an isolated module" invariant; `server.py` needs **zero** route changes —
the router is already mounted).

### Request model (new, in `multi.py`)

```python
class MultiCustomRequest(BaseModel):
    videos:          List[MultiVideoSource] = Field(min_length=1, max_length=MAX_MULTI_VIDEOS)
    system_prompt:   str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    user_prompt:     str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    title:           str = "Combined Notes"      # title of the combined document
    category:        Optional[str] = None        # bookkeeping only, like /custom
    language:        Optional[str] = None        # spoken/ASR language, job-wide
    output_language: str = "en"                  # written-notes language, job-wide
```

- Reuses `MultiVideoSource` verbatim → per-video optional `kind` + `title`, YouTube
  auto-detection from host, explicit `kind` wins — identical rules to `/multi`.
- `MAX_PROMPT_CHARS`: read `VIDEOARM_MAX_PROMPT_CHARS` (default 20000) at the top of
  `multi.py`, same pattern as `MAX_MULTI_VIDEOS` there. (Do **not** import it from
  `server.py` at module top — that back-import is circular by design; env read is the
  established idiom in this module.)
- Deliberately **no** `domain` / `intent`: the single `/custom` endpoint suppresses
  both (passes `""`) because the caller's prompts own the instruction space. Same here.
- Response: existing `JobAccepted` (`{job_id, status:"queued"}`). Poll/download reuse
  `/v1/jobs/*` untouched.

### Handler (mirrors `submit_multi`, ~15 lines)

1. Build `sources` list exactly as `submit_multi` does (kind resolution, order preserved).
2. `out_lang = normalize_code(req.output_language)`.
3. `srv._insert(job_id, status="queued", source="multi", title=req.title,
   language=req.language, output_language=out_lang, category=req.category,
   system_prompt=req.system_prompt, user_prompt=req.user_prompt,
   sources_json=json.dumps(sources))` — note `source="multi"` (not a new value), so
   every existing code path that branches on `source` keeps working, including recovery.
4. `srv._executor.submit(_run_multi_job, job_id, sources, req.title, req.language,
   "", "", out_lang, req.system_prompt, req.user_prompt)`.

### OpenAPI

FastAPI generates the schema automatically from the model. Add a docstring with the
same shape as `submit_custom`'s (description + two curl examples: mixed URL/YouTube
stack). Update the endpoint listing in `server.py`'s module docstring header. Swagger
UI at `/docs` then documents it with zero extra work.

---

## 2. Backend processing & prompt semantics

### Changes (the whole diff besides the route)

- `_run_multi_job(job_id, sources, title, language, domain, intent, output_language,
  system_prompt=None, user_prompt=None)` — two new **defaulted** trailing params,
  forwarded into the existing `MultiVideoSummarizer().summarize(...)` call
  (which already has the parameters). Existing callers are unaffected.
- `resume_multi_job`: append `row["system_prompt"], row["user_prompt"]` to its
  `_run_multi_job(...)` call. Lecture multi jobs have those columns NULL → `None` →
  identical behaviour to today. One code path for both flavors; no branching.

### Prompt application semantics (all existing behaviour, stated for the record)

- **`system_prompt` applies identically to every video and every segment.** In
  `summarize_body`, it replaces `_SYSTEM_NOTE_TAKER` (and the domain block). When
  `output_language != "en"`, the language directive is appended — same rule as single
  `/custom`, applied per video, so the whole combined document comes out in one language.
- **`user_prompt` applies per segment of every video**, with that segment's
  transcript/visual-notes/figures auto-appended via `_CUSTOM_DATA_BLOCK`. The caller
  never supplies content, only instructions — consistent across all N videos.
- **Combined document shape is owned by the pipeline, not the prompts:** job-wide
  `title` on the title page + TOC; each video is one numbered `\section` titled by
  per-video `title` → YouTube title → `"Video i"` (existing fallback chain in the
  downloader/summarizer); stray `\section`s emitted by the model are demoted to
  `\subsection` (existing regex in `MultiVideoSummarizer`); `\clearpage` between
  videos; figures namespaced `vNN_` per video. Compile happens **once** via
  `build_and_compile` with the job's `output_language` → one PDF + one `.tex`.
- `category` is stored on the job row and ignored by generation — same as `/custom`.

---

## 3. Validation, errors, ordering, restart

All inherited; nothing new to build:

| Concern | Behaviour (source) |
|---|---|
| Auth | `401` via `_require_key` (existing) |
| Empty / >8 `videos`, bad URL, bad `kind` | `422` (pydantic on `MultiVideoSource` + `Field` bounds) |
| Missing/empty/oversized prompts | `422` (pydantic `Field(min_length=1, max_length=MAX_PROMPT_CHARS)`) |
| Unknown `output_language` | silent fallback to English via `normalize_code` — never a 422 (existing contract) |
| Ordering | `sources` list order = section order (existing; JSON array order is preserved through `sources_json`) |
| All-or-nothing | any download/pipeline failure raises `RuntimeError(f"Video {i}/{N} ({url}): …")` and fails the whole job (existing `_run_multi_job`) |
| Progress / ETA | segments aggregated across all videos, probed up front (existing `probe_segment_count`) |
| Restart — job was `queued` | `_recover` routes `source=="multi"` → `resume_multi_job` → re-downloads URL/YouTube, now also re-passing the persisted prompts |
| Restart — job was `downloading`/`processing` | failed with `"Interrupted by server restart"` (existing) |
| Temp cleanup | downloaded files + yt-dlp dirs removed in `finally` (existing) |

Explicit **non-goal**: no `/multi/custom/upload` variant. The consuming app uploads to
Bunny Storage and submits public URLs; URL + YouTube covers the contract. Can be added
later exactly the way `/multi/upload` mirrors `/multi` if ever needed.

---

## 4. Compatibility

- **Purely additive route.** No existing request/response model, endpoint, status
  value, or download URL changes.
- `/v1/summarize/custom` (single) — untouched.
- `/v1/summarize/multi` (+ `/upload`) — untouched; their `_run_multi_job` calls hit the
  new defaulted params as `None`, which is byte-for-byte today's behaviour.
- DB — no migration; columns exist. Old rows recover cleanly (NULL prompts → `None`).
- Clients polling `/v1/jobs/{id}` see the standard `JobStatus`; nothing to migrate.
- `Jard-Spark/` snapshot: apply the same (renamed) diff there if it should track, or
  regenerate the snapshot after merge — it does not pick up changes automatically.

---

## 5. Tests

Repo convention is plain-python test scripts (`test_lecture.py`,
`test_output_language.py`), so:

**`test_multi_custom.py` — offline, no GPU needed** (FastAPI `TestClient`; point
`VIDEOARM_DB_PATH` / `VIDEOARM_OUTPUT_DIR` at a temp dir via env **before** importing
`videoarm.api.server`, since import initializes the DB and executor):

1. *Auth*: no/wrong `X-API-Key` → `401`.
2. *Validation*: empty `videos` → 422; 9 videos → 422; missing/empty/20001-char
   `system_prompt` or `user_prompt` → 422; invalid URL → 422; bad `kind` → 422.
3. *Submit persists correctly*: `202` + job row has `source="multi"`, both prompts,
   `category`, normalized `output_language` (e.g. `"Arabic"` → `ar`, garbage → `en`),
   and `sources_json` preserving order + auto-detected kinds (youtu.be → `youtube`,
   CDN URL → `url`, explicit `kind` wins).
4. *Prompt plumbing* (monkeypatch `MultiVideoSummarizer.summarize` + the downloaders):
   worker passes `system_prompt`/`user_prompt`/`output_language` through; videos arrive
   in order with correct titles.
5. *All-or-nothing*: make video 2's download raise → job `failed`, error starts with
   `"Video 2/3 ("` ; temp files cleaned up.
6. *Restart recovery*: insert a queued `source="multi"` row **with** prompts, run
   `server._recover()` with `resume_multi_job` monkeypatched → prompts included; a
   queued lecture-multi row (NULL prompts) still resumes with `None`.
7. *Regression*: `/v1/summarize/multi` and `/v1/summarize/custom` submits still return
   202 with unchanged persisted shapes.

**Live smoke (GPU, manual — extend the §9-style smoke used for `/multi`):** submit two
short real videos with a distinctive custom prompt pair (e.g. "meeting minutes"),
poll to `done` verifying `segments_total` = sum of parts and ETA appears after segment
1, download `/pdf` + `/tex`, assert: one document, two `\section`s in submission
order, prompt style visibly applied in both sections, `.tex` uses `vNN_`-prefixed
figure paths. Then a failure run with one dead URL → `failed` + `"Video 1/2"` message.

---

## 6. Documentation updates

| Doc | Change |
|---|---|
| `multi-video-api-docs.md` | New section for `/multi/custom` (request table, prompt semantics incl. "tell the model LaTeX body only", curl + TS examples, limits row) |
| `jard-api-handoff-adam.md` | New §3.7 + row in the limits table (this is Adam's live reference — unblocks his app's multi-custom path) |
| `custom-endpoint-api-docs.md` | One-paragraph cross-reference ("for stacks, use /multi/custom") |
| `Jard-Spark/README.md` | New §9.7 mirroring the endpoint |
| `server.py` module docstring | Add the route to the endpoint listing |
| Memory (`memory/project_videoarm.md`) | Update endpoint list after implementation |

---

## Deploy checklist (after implementation is approved & done)

1. **Commit the currently-uncommitted multi-video work first** (`multi.py`,
   `multi_summarizer.py`, `server.py`/`lecture_summarizer.py` edits) — it is the
   prerequisite for this feature and is already live-but-uncommitted.
2. Implement + run `test_multi_custom.py` offline.
3. Restart only the API server (`bash start_api.sh`) — model servers untouched;
   queued jobs survive, in-flight jobs fail with the standard restart message.
4. Live smoke test through the public IP (currently `34.71.177.8`; verify via GCP
   metadata first — it rotates), from off-box (GCP hairpinning).
5. Update the docs above with the verified examples; send Adam the handoff update.
