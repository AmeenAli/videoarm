# Jard.ai — How Screenshots (Figures) in the Output Work

> Answers "where do the images in the PDF come from, and do I have to ask for them?"
> **Owner:** Ameen. **Last updated:** 29 July 2026.

---

## TL;DR

- The images embedded in the PDFs are **real screenshots — actual frames extracted from
  the submitted video**. Nothing is AI-generated or drawn; the AI only *chooses* which
  frames are worth keeping and *writes the captions*.
- On the standard endpoints (`/v1/summarize`, `/youtube`, `/upload`, `/multi`) this is
  **fully automatic**. There is no flag to send, nothing to request, and no tool or
  artifact instruction needed from your side.
- On the **custom-prompt endpoints** (`/v1/summarize/custom`) the screenshots are still
  extracted and offered to the model automatically, but whether they appear in the
  final document depends on your prompts — see [Custom endpoints](#custom-endpoints)
  for the one line to add.

---

## How it actually works (per 5-minute segment)

For every ~5-minute segment of every video, the server:

1. **Samples 15 frames** from the segment (at 480px — sharp enough for print, and the
   source is capped at 720p anyway).
2. **Asks the vision model to judge them**: it selects **at most 3** frames that contain
   information a transcript can't carry — diagrams, plots/graphs, geometric
   constructions, circuits, flowcharts, algorithm visualisations. It deliberately
   **rejects** text-only slides (the transcript already has that content), blurry or
   duplicate frames, and shots of the lecturer/audience.
3. **Copies the chosen frames** into the job's output as image files and hands the model
   writing that segment's notes the list of `path + caption` pairs.
4. The note-writing model **places each figure** (`\includegraphics` in a `figure`
   block) right after the paragraph that discusses what it shows, with the AI-written
   caption.
5. The final compile **embeds the images into the PDF**.

So in your terms: the screenshot *files* are added by the pipeline (deterministic
code), the *selection and captions* are model judgment, and the *placement* is done by
the model while writing the notes. None of it needs an instruction from the API caller.

For **multi-video jobs**, the same thing happens per video, and figure files are
namespaced per video internally (`v01_…`, `v02_…`) so screenshots from different parts
of a stack never collide in the combined document.

---

## Custom endpoints — the one thing to know

On `/v1/summarize/custom`, frame extraction and selection run automatically, and the
selected screenshots (path + caption) are appended to your `user_prompt` alongside the
transcript. Since 29 Jul 2026 the **selector judges frames against your prompts** rather
than the lecture rubric — so a film-analysis job gets key shots and compositions, not
just diagrams. (Before this fix the lecture-only selector rejected every frame of
non-lecture videos, which is why custom jobs came back with zero screenshots.) But since
**your** `system_prompt` replaces the built-in note-taker instructions, the model only
has a light hint about what to do with them.

To make figure behaviour deterministic, say it explicitly in your `system_prompt`:

- **Want screenshots in the output** (recommended):

  > "When figures are listed, include each one where relevant using
  > `\begin{figure}[H] \centering \includegraphics[width=0.85\linewidth]{<path>}
  > \caption{<caption>} \end{figure}`, with path and caption exactly as given."

- **Don't want screenshots** (e.g. text-only meeting minutes):

  > "Ignore the FIGURES section; do not include any `\includegraphics` commands."

---

## PDF vs `.tex` — one gotcha

The **PDF has the screenshots embedded** — it is self-contained, always safe to serve.

The **`.tex` source references the images by relative path** (`figures/…`), and since
29 Jul 2026 the API serves those files too:

- `GET /v1/jobs/{job_id}/images` — JSON list of every screenshot file (filename, bytes, url)
- `GET /v1/jobs/{job_id}/images/{filename}` — the image bytes

To recompile or preview the `.tex` yourself: download the `.tex`, list the images,
save each file into a `figures/` directory next to the `.tex` — the relative paths
then resolve. Full request/response examples are in `ENDPOINTS.md`.

---

## Quick reference

| Question | Answer |
|---|---|
| Are images AI-generated? | No — real frames from the submitted video |
| Who picks them? | The vision model (diagram/plot-worthy only; text slides skipped) |
| Who writes captions? | The model, from the frame content |
| Do I need to request them? | No — automatic on all endpoints |
| Can prompts control them? | Yes, on `/custom` — one explicit line in `system_prompt` (see above) |
| How many? | Up to 3 per 5-minute segment |
| Is there an API flag to toggle figures? | Not currently — the custom prompt is the control; ask if you need a flag |
| Are images in the `.tex` download? | Referenced by path only; embedded in the PDF |
| Can I download the image files? | Yes — `GET /v1/jobs/{id}/images` (list) and `…/images/{filename}` (file) |
| What picks frames on `/custom`? | The selector reads your prompts and picks frames relevant to *your* task |

*Questions — Ameen.*
