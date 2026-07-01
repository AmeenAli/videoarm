# Jard.ai — Cost & Pricing Review

**Prepared for:** Client
**Service:** Jard.ai Automated Lecture-Notes API (managed / hosted)
**Date:** 23 June 2026
**Status:** Proposal for discussion

---

## 1. Summary

Jard.ai turns lecture and long-form videos into structured, publication-quality
notes (LaTeX source + typeset PDF). This document sets out what it costs to run
the service, the pricing model we propose, and what your monthly bill would look
like at your expected volume of **100–500 videos per month**.

In short: a **flat platform fee covers the dedicated GPU infrastructure and
support**, and a **per-video rate covers processing**. Your effective cost per
video falls as your volume grows.

---

## 2. What the service includes

| | |
|---|---|
| **Processing** | Video → ASR transcription → visual figure extraction → structured notes synthesis |
| **Outputs** | LaTeX source (`.tex`) and a typeset PDF per video |
| **Inputs** | Direct video URL, file upload, or YouTube link |
| **API** | REST API with job submission, status polling, and result download |
| **Infrastructure** | Dedicated NVIDIA A100 80GB GPU cluster (model serving + audio) |
| **Reliability** | Persistent job queue — submitted jobs survive restarts and are retried |
| **Support** | Monitoring, maintenance, model updates, and email support |

A typical **40-minute lecture is processed in ~10 minutes**, and the platform
processes **multiple videos concurrently**, so throughput is not a constraint at
your volume.

---

## 3. Cost basis (what drives the price)

The dominant cost is dedicated GPU compute. The service runs on NVIDIA A100 80GB
GPUs on Google Cloud, reserved so capacity is always available to you.

| Cost component | Notes | Indicative monthly |
|---|---|---|
| GPU compute (reserved) | A100 80GB cluster, committed-use rate | ~$6,500 – $9,300 |
| Storage & bandwidth | Output PDFs, video transfer | ~$400 – $650 |
| Operations & support | Monitoring, updates, maintenance | included |

> *GPU compute figures are based on Google Cloud committed-use pricing for A100
> 80GB instances and are indicative; final figures are confirmed against live
> cloud pricing at contract time.*

The **marginal cost to process one additional video is low (~$3–4)** once the
platform is provisioned — so the pricing model below is structured to pass that
efficiency on to you as your volume grows.

---

## 4. Proposed pricing model

A two-part structure keeps unit costs fair across volumes:

- **Platform fee** — a flat monthly fee covering reserved GPU capacity, the API,
  storage, monitoring, and support.
- **Per-video fee** — covers processing of each video.

| Component | Price |
|---|---|
| Platform fee | **$2,000 / month** |
| Per-video processing | **$8 / video** |

Includes notes generation (LaTeX + PDF), URL/upload/YouTube ingestion, and
result storage for 30 days.

---

## 5. Your monthly cost at expected volume

| Videos / month | Platform fee | Processing | **Total / month** | **Effective $/video** |
|---:|---:|---:|---:|---:|
| 100 | $2,000 | $800 | **$2,800** | $28.00 |
| 200 | $2,000 | $1,600 | **$3,600** | $18.00 |
| 300 | $2,000 | $2,400 | **$4,400** | $14.67 |
| 500 | $2,000 | $4,000 | **$6,000** | $12.00 |

As your volume increases, the fixed platform fee spreads across more videos and
your effective cost per video drops.

---

## 6. As you scale: dedicated-capacity plan

If your volume grows beyond ~1,000 videos/month, a **flat dedicated-capacity
plan** becomes more economical than per-video pricing: a single fixed monthly
fee for the full reserved GPU cluster, with no per-video charge up to its
capacity (several thousand videos/month). We're happy to quote this when the
time comes.

---

## 7. Assumptions & notes

- Pricing assumes lecture/long-form videos with a typical length up to ~90
  minutes; unusually long videos may be quoted separately.
- YouTube ingestion depends on source availability; some videos may require a
  direct file or URL.
- Cloud-compute figures are indicative and confirmed against live Google Cloud
  pricing at contract time.
- A minimum 3-month term is proposed so reserved GPU capacity can be committed
  at the lower committed-use rate reflected above.
- All prices in USD, exclusive of applicable taxes.

---

*Prepared by Jard.ai. This proposal is for discussion and is not a binding quote.*
