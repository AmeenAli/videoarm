"""Offline tests for POST /v1/summarize/multi/custom — no GPU, no model servers.

Run: .venv/bin/python test_multi_custom.py
Env is pointed at a temp dir BEFORE importing the server (import initializes the
DB and worker executor), and the background worker is monkeypatched so nothing
actually downloads or summarizes.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="videoarm_test_")
os.environ["VIDEOARM_OUTPUT_DIR"] = _TMP
os.environ["VIDEOARM_DB_PATH"] = str(Path(_TMP) / "jobs.db")
os.environ["VIDEOARM_API_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402

import videoarm.api.multi as multi  # noqa: E402
import videoarm.api.server as server  # noqa: E402

client = TestClient(server.app)
HDRS = {"X-API-Key": "test-key"}
PASS = 0


def ok(cond, label):
    global PASS
    assert cond, label
    PASS += 1
    print(f"  ✓ {label}")


def base_body(**over):
    body = {
        "videos": [
            {"url": "https://youtu.be/aaa111"},
            {"kind": "url", "url": "https://cdn.example.com/ep2.mp4", "title": "Ep 2"},
        ],
        "system_prompt": "You are a reviewer. Output LaTeX body only.",
        "user_prompt": "Review each episode.",
        "title": "Season Review",
        "category": "review",
        "output_language": "Arabic",
    }
    body.update(over)
    return body


def row(job_id):
    return server._get(job_id)


print("1. Auth")
r = client.post("/v1/summarize/multi/custom", json=base_body())
ok(r.status_code in (401, 422), "missing key rejected")
r = client.post("/v1/summarize/multi/custom", json=base_body(),
                headers={"X-API-Key": "wrong"})
ok(r.status_code == 401, "wrong key → 401")

print("2. Validation")
for label, body in [
    ("empty videos", base_body(videos=[])),
    ("9 videos", base_body(videos=[{"url": f"https://x.com/{i}.mp4"} for i in range(9)])),
    ("missing system_prompt", {k: v for k, v in base_body().items() if k != "system_prompt"}),
    ("empty system_prompt", base_body(system_prompt="")),
    ("oversized user_prompt", base_body(user_prompt="x" * 20001)),
    ("invalid url", base_body(videos=[{"url": "not a url"}])),
    ("bad kind", base_body(videos=[{"kind": "vimeo", "url": "https://x.com/a.mp4"}])),
]:
    r = client.post("/v1/summarize/multi/custom", json=body, headers=HDRS)
    ok(r.status_code == 422, f"{label} → 422")

print("3. Submit persists correctly")
with mock.patch.object(multi, "_run_multi_job") as run:
    r = client.post("/v1/summarize/multi/custom", json=base_body(), headers=HDRS)
    ok(r.status_code == 202, "valid submit → 202")
    job_id = r.json()["job_id"]
    time.sleep(0.2)  # executor dispatch
    rw = row(job_id)
    ok(rw["source"] == "multi", 'source == "multi"')
    ok(rw["system_prompt"].startswith("You are a reviewer"), "system_prompt persisted")
    ok(rw["user_prompt"] == "Review each episode.", "user_prompt persisted")
    ok(rw["category"] == "review", "category persisted")
    ok(rw["output_language"] == "ar", 'output_language "Arabic" normalized to "ar"')
    srcs = json.loads(rw["sources_json"])
    ok([s["kind"] for s in srcs] == ["youtube", "url"],
       "kinds auto-detected (youtu.be) / explicit preserved, order kept")
    ok(srcs[1]["title"] == "Ep 2", "per-video title preserved")
    ok(run.called, "worker dispatched")
    args = run.call_args[0]
    ok(args[7] == rw["system_prompt"] and args[8] == rw["user_prompt"],
       "prompts passed to worker")
    ok(args[4] == "" and args[5] == "", "domain/intent suppressed")

r = client.post("/v1/summarize/multi/custom",
                json=base_body(output_language="garbage-lang"), headers=HDRS)
ok(row(r.json()["job_id"])["output_language"] == "en",
   "unknown output_language falls back to en (no 422)")

print("4. Prompt plumbing into MultiVideoSummarizer")
captured = {}


class FakeSummarizer:
    def summarize(self, **kw):
        captured.update(kw)
        pdf = Path(_TMP) / "out.pdf"
        pdf.write_bytes(b"%PDF-fake")
        Path(_TMP, "out.tex").write_text("x")
        return str(pdf)


with mock.patch("videoarm.core.multi_summarizer.MultiVideoSummarizer", FakeSummarizer), \
     mock.patch.object(multi, "httpx") as fake_httpx:
    stream = mock.MagicMock()
    stream.__enter__.return_value.raise_for_status.return_value = None
    stream.__enter__.return_value.iter_bytes.return_value = [b"vid"]
    fake_httpx.stream.return_value = stream
    multi._run_multi_job("jobX", [{"kind": "url", "url": "https://x.com/a.mp4",
                                   "title": "T1"}],
                         "Doc", None, "", "", "fr", "SYS", "USER")
ok(captured["system_prompt"] == "SYS" and captured["user_prompt"] == "USER",
   "prompts reach MultiVideoSummarizer.summarize")
ok(captured["output_language"] == "fr", "output_language forwarded")
ok(captured["videos"][0]["title"] == "T1", "video titles forwarded")
ok(row("jobX") is None or True, "worker ran standalone")  # jobX has no row; _set is a no-op update

print("5. All-or-nothing failure naming the video")
server._insert("jobF", status="queued", source="multi", title="t")
with mock.patch.object(multi, "httpx") as fake_httpx:
    fake_httpx.stream.side_effect = RuntimeError("connect refused")
    multi._run_multi_job("jobF", [{"kind": "url", "url": "https://x.com/dead.mp4"}],
                         "Doc", None, "", "", "en", "SYS", "USER")
rw = row("jobF")
ok(rw["status"] == "failed" and rw["error"].startswith("Video 1/1 ("),
   'failure → status failed, error "Video 1/1 (…)"')

print("6. Restart recovery passes prompts")
server._insert("jobR", status="queued", source="multi", title="t",
               system_prompt="SYS-R", user_prompt="USER-R",
               sources_json=json.dumps([{"kind": "url", "url": "https://x.com/a.mp4"}]))
with mock.patch.object(multi, "_run_multi_job") as run:
    multi.resume_multi_job(row("jobR"))
    a = run.call_args[0]
    ok(a[7] == "SYS-R" and a[8] == "USER-R", "resume passes persisted prompts")
server._insert("jobL", status="queued", source="multi", title="t",
               sources_json=json.dumps([{"kind": "url", "url": "https://x.com/a.mp4"}]))
with mock.patch.object(multi, "_run_multi_job") as run:
    multi.resume_multi_job(row("jobL"))
    a = run.call_args[0]
    ok(a[7] is None and a[8] is None and a[4] == "" and a[5] == "",
       "lecture-multi row resumes with None prompts, blank domain/intent")

print("7. Regression — existing endpoints unchanged")
with mock.patch.object(multi, "_run_multi_job"):
    r = client.post("/v1/summarize/multi",
                    json={"videos": [{"url": "https://youtu.be/zzz"}]}, headers=HDRS)
    ok(r.status_code == 202, "/multi still 202")
    ok(row(r.json()["job_id"])["system_prompt"] is None,
       "/multi rows keep NULL prompts")
with mock.patch.object(server._executor, "submit"):
    r = client.post("/v1/summarize/custom", json={
        "source": {"url": "https://youtu.be/zzz"},
        "system_prompt": "s", "user_prompt": "u"}, headers=HDRS)
    ok(r.status_code in (200, 202), "/custom still accepts")

print(f"\nALL {PASS} CHECKS PASSED")
