import pytest

from conftest import transcript
from scope.agents.estimator import compute_estimate
from scope.config import Settings
from scope.models import Deliverable, EstimateTask, Feasibility
from scope.transcripts import chunk_lines, client_speakers, parse_transcript, verify_quote


# ------------------------------------------------------------------ transcripts
def test_parse_meta_lines_and_speakers():
    t = parse_transcript("meeting-a", transcript("meeting_a_bayside_discovery.txt"))
    assert t.date == "2026-10-05" and "Bayside" in t.title
    assert len(t.lines) > 90
    assert t.lines[0].timestamp == "00:00:04" and t.lines[0].speaker == "Arjun Mehta"
    assert client_speakers(t) == {"Rachel Kim", "Dev Shah"}


def test_long_transcript_is_chunked_with_overlap():
    t = parse_transcript("meeting-a", transcript("meeting_a_bayside_discovery.txt"))
    chunks = chunk_lines(t, max_chars=4000, overlap_lines=3)
    assert len(chunks) >= 3
    assert [l.n for l in chunks[0][-3:]] == [l.n for l in chunks[1][:3]]      # overlap
    assert {l.n for c in chunks for l in c} == {l.n for l in t.lines}       # nothing lost


def test_quote_verification():
    t = parse_transcript("meeting-a", transcript("meeting_a_bayside_discovery.txt"))
    line = next(l for l in t.lines if "launch by March 1st" in l.text)
    ok = verify_quote(t, line.n, "So launch by March 1st at the latest.")
    assert ok.verified and ok.speaker == "Rachel Kim"
    near = verify_quote(t, line.n + 2, "launch by March 1st at the latest")   # wrong line, found nearby
    assert near.verified and near.line == line.n
    bad = verify_quote(t, line.n, "We need blockchain integration for invoices")
    assert not bad.verified


# --------------------------------------------------------------------- estimate
def test_estimate_arithmetic_is_deterministic():
    s = Settings(hourly_rate=100, currency="USD", base_contingency_pct=10, contingency_per_unclear_pct=5)
    d = [Deliverable(id="D-01", title="A", description="", requirement_ids=["REQ-001"], approach="configuration", acceptance_criteria=["x"]),
         Deliverable(id="D-02", title="B", description="", requirement_ids=["REQ-002"], approach="to_be_confirmed", acceptance_criteria=["x"])]
    tasks = [EstimateTask(id="T-01", deliverable_id="D-01", name="a", category="configuration", low=2, likely=4, high=6, rationale=""),
             EstimateTask(id="T-02", deliverable_id="D-02", name="b", category="backend", low=6, likely=10, high=20, rationale="")]
    f = [Feasibility(requirement_id="REQ-002", classification="needs_clarification", rationale="", kb_question="", kb_status="insufficient_evidence", verified=False)]
    e = compute_estimate(tasks, d, f, s)
    assert (e.total_low, e.total_likely, e.total_high) == (8, 14, 26)
    assert e.total_expected == pytest.approx(4 + 11, abs=0.05)       # PERT: (2+16+6)/6=4, (6+40+20)/6=11
    assert e.contingency_pct == 15                                    # 10 base + 5 for one unclear deliverable
    assert e.total_with_contingency == pytest.approx(17.25, abs=0.06)
    assert e.price_expected == round(17.25 * 100)
    assert e.per_deliverable["D-02"]["likely"] == 10


# --------------------------------------------------------------------- pipeline
def load(client):
    client.post("/demo/load")
    return {p["name"]: p["id"] for p in client.get("/projects").json()["projects"]}


def run(svc, meeting_id):
    job = svc.orchestrator.start(meeting_id)
    vid = svc.orchestrator.run_job(job)
    assert vid, svc.store.job(job)["error"]
    return vid


def test_baseline_pipeline_produces_traceable_scope(client, svc, kb):
    load(client)
    vid = run(svc, "meeting-a")
    v = client.get(f"/versions/{vid}").json()
    pkg = v["payload"]
    reqs = pkg["requirements"]["requirements"]
    assert reqs and all(r["sources"] for r in reqs)                         # every requirement links to transcript lines
    assert all(s["meeting_id"] == "meeting-a" for r in reqs for s in r["sources"])
    assert kb.questions and len(pkg["feasibility"]) == len([r for r in reqs if r["status"] != "removed"])  # Task 1 called per requirement
    assert any(f["verified"] for f in pkg["feasibility"]) and any(f["classification"] == "needs_clarification" for f in pkg["feasibility"])
    assert pkg["scope"]["deliverables"] and pkg["estimate"]["tasks"]
    assert pkg["estimate"]["total_with_contingency"] > 0
    assert len(pkg["extracted_items"]) > 5
    assert "## Estimate" in v["markdown"] and "DRAFT" in v["markdown"]
    job = client.get(f"/projects/{v['project_id']}").json()["jobs"][0]
    assert [s["status"] for s in job["stages"]] == ["done"] * 7


def test_send_requires_approval_and_edits_invalidate_it(client, svc):
    load(client)
    vid = run(svc, "meeting-a")
    r = client.post(f"/versions/{vid}/send", json={"sender": "kavya"})
    assert r.status_code == 409 and "approve" in r.json()["error"]["message"]
    assert client.post(f"/versions/{vid}/approve", json={"reviewer": ""}).status_code == 422
    assert client.post(f"/versions/{vid}/approve", json={"reviewer": "Manoj"}).json()["status"] == "approved"
    task = client.get(f"/versions/{vid}").json()["payload"]["estimate"]["tasks"][0]
    edited = client.patch(f"/versions/{vid}/tasks/{task['id']}", json={"low": 1, "likely": 2, "high": 3, "editor": "Manoj", "reason": "known work"}).json()
    assert edited["status"] == "draft" and edited["approved_by"] is None          # approval dropped by the edit
    assert client.post(f"/versions/{vid}/send", json={"sender": "kavya"}).status_code == 409
    client.post(f"/versions/{vid}/approve", json={"reviewer": "Manoj"})
    sent = client.post(f"/versions/{vid}/send", json={"sender": "kavya"}).json()
    assert sent["status"] == "sent" and sent["sent_path"].endswith("-v1.md")
    assert client.post(f"/versions/{vid}/send", json={"sender": "kavya"}).status_code == 409   # no double send
    assert client.patch(f"/versions/{vid}/tasks/{task['id']}", json={"low": 1, "likely": 2, "high": 3, "editor": "x"}).status_code == 409
    actions = [a["action"] for a in client.get("/audit").json()["entries"]]
    assert {"version_approved", "version_edited", "version_sent"} <= set(actions)


def test_blocking_issues_must_be_acknowledged(client, svc):
    load(client)
    svc.llm.overrides["record_narrative"] = lambda user: {
        "executive_summary": "This will cost $9,500 and take 80 hours.", "client_problem": "x", "solution_overview": "y", "next_steps": ["a"]}
    vid = run(svc, "meeting-a")
    issues = client.get(f"/versions/{vid}").json()["payload"]["review_issues"]
    assert any(i["severity"] == "block" and "$9,500" in i["message"] for i in issues)
    r = client.post(f"/versions/{vid}/approve", json={"reviewer": "Manoj"})
    assert r.status_code == 422
    assert client.post(f"/versions/{vid}/approve", json={"reviewer": "Manoj", "acknowledge_blocks": True}).status_code == 200


def test_followup_requires_an_approved_baseline(client, svc):
    load(client)
    run(svc, "meeting-a")                     # draft only
    r = client.post("/meetings/meeting-c/analyse")
    assert r.status_code == 409 and "approve" in r.json()["error"]["message"].lower()


def test_followup_change_report_against_approved_baseline(client, svc):
    load(client)
    base_id = run(svc, "meeting-a")
    client.post(f"/versions/{base_id}/approve", json={"reviewer": "Manoj"})
    base = client.get(f"/versions/{base_id}").json()["payload"]
    reqs = base["requirements"]["requirements"]
    removed_id, modified_id = reqs[0]["id"], reqs[1]["id"]
    kept_deliverables = [d for d in base["scope"]["deliverables"] if not {removed_id, modified_id} & set(d["requirement_ids"])]

    def changes(user):
        import re
        ids = re.findall(r"^(C\d+-\d+) \|", user, re.M)
        return {"changes": [
            {"change_type": "removed", "baseline_requirement_id": removed_id, "title": "drop", "item_ids": ids[:1], "explanation": "client dropped it"},
            {"change_type": "modified", "baseline_requirement_id": modified_id, "title": "filters", "new_description": "Filters: category, distance, languages spoken",
             "item_ids": ids[1:2], "explanation": "rating filter out, language filter in"},
            {"change_type": "added", "title": "Get three quotes", "new_description": "Homeowner sends one request to up to three contractors",
             "item_ids": ids[2:3], "explanation": "new request"}],
            "schedule_change": {"before": "March 1st, 2027", "after": "February 10th, 2027", "item_ids": ids[3:4]}, "other_changes": []}

    svc.llm.overrides["record_changes"] = changes
    vid = run(svc, "meeting-c")
    v = client.get(f"/versions/{vid}").json()
    pkg = v["payload"]
    assert v["kind"] == "revision" and v["based_on"] == base_id
    cr = pkg["change_report"]
    kinds = {i["change_type"]: i for i in cr["items"]}
    assert set(kinds) == {"removed", "modified", "added"}
    assert kinds["added"]["out_of_scope"] is True and kinds["removed"]["out_of_scope"] is False
    assert cr["schedule_change"] == {"before": "March 1st, 2027", "after": "February 10th, 2027"}
    new_req = next(r for r in pkg["requirements"]["requirements"] if r["title"] == "Get three quotes")
    assert new_req["out_of_scope_change"] and new_req["added_in_meeting"] == "meeting-c"
    assert next(r for r in pkg["requirements"]["requirements"] if r["id"] == removed_id)["status"] == "removed"
    # untouched deliverables keep their approved tasks verbatim
    for d in kept_deliverables:
        before = [t for t in base["estimate"]["tasks"] if t["deliverable_id"] == d["id"] and t["category"] != "project_management"]
        after = [t for t in pkg["estimate"]["tasks"] if t["deliverable_id"] == d["id"] and t["category"] != "project_management"]
        assert before == after
    assert all(removed_id not in d["requirement_ids"] for d in pkg["scope"]["deliverables"])
    assert "What changed since version 1" in v["markdown"] and "outside the approved scope" in v["markdown"]
    assert v["status"] == "draft"                                               # revision needs its own approval


def test_knowledge_brain_down_blocks_approval(client, svc, kb):
    load(client)
    kb.mode = "down"
    vid = run(svc, "meeting-a")
    pkg = client.get(f"/versions/{vid}").json()["payload"]
    assert all(f["kb_status"] == "unavailable" and not f["verified"] for f in pkg["feasibility"])
    assert any(i["severity"] == "block" and "Knowledge Brain unavailable" in i["message"] for i in pkg["review_issues"])
    assert client.post(f"/versions/{vid}/approve", json={"reviewer": "Manoj"}).status_code == 422


def test_feasibility_cannot_claim_existing_feature_without_evidence(client, svc):
    load(client)
    svc.llm.overrides["record_feasibility"] = lambda user: {"results": [
        {"requirement_id": rid, "classification": "existing_configuration", "rationale": "sure", "supporting_urls": []}
        for rid in __import__("re").findall(r'<requirement id="(REQ-\d+)"', user)]}
    job = svc.orchestrator.start("meeting-a")
    assert svc.orchestrator.run_job(job) is None
    j = svc.store.job(job)
    assert j["status"] == "failed" and "feasibility" in j["error"]
    assert next(s for s in j["stages"] if s["name"] == "feasibility")["status"] == "failed"


def test_api_validation_and_samples(client):
    assert client.post("/projects", json={"name": "x"}).status_code == 422
    assert len(client.get("/samples").json()["samples"]) == 3
    pid = client.post("/projects", json={"name": "Demo", "client": "Demo Co"}).json()["id"]
    r = client.post(f"/projects/{pid}/meetings", json={"title": "Kickoff", "date": "2026-10-01", "kind": "initial", "transcript": "too short"})
    assert r.status_code == 422
