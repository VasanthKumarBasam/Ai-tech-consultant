import asyncio

import pytest
from fastapi.testclient import TestClient

from kb.api import create_app
from kb.llm import FakeLLM


@pytest.fixture
def client(services):
    asyncio.run(services.pipeline.run("incremental"))
    app = create_app(services)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["articles_active"] == 4 and r["image"] == 5
    assert r["llm_provider"] == "fake"


def test_query_answered_with_citations(client):
    r = client.post("/query", json={"question": "How do I refund a payment to a member?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"
    cited = [s for s in body["sources"] if s["cited"]]
    assert cited and "refund" in cited[0]["url"]
    assert body["needs_human"] is False and body["escalation_id"] is None
    assert r.headers["x-request-id"] == body["request_id"]


def test_unanswerable_question_escalates(client):
    r = client.post("/query", json={"question": "Does the platform integrate with quantum blockchain teleportation?"}).json()
    assert r["status"] == "insufficient_evidence"
    assert r["needs_human"] is True and r["escalation_id"]
    escs = client.get("/escalations?status=open").json()["escalations"]
    assert escs[0]["question"].startswith("Does the platform")
    # same question again -> deduplicated onto the same open escalation
    r2 = client.post("/query", json={"question": "Does the platform integrate with quantum blockchain teleportation?"}).json()
    assert r2["escalation_id"] == r["escalation_id"]


def test_resolved_escalation_becomes_retrievable_expert_knowledge(client):
    q = "Can members pay by bank transfer on invoices?"
    r = client.post("/query", json={"question": q}).json()
    assert r["status"] == "insufficient_evidence"
    eid = r["escalation_id"]
    res = client.post(f"/escalations/{eid}/resolve", json={
        "answer": "Bank transfer is not a built-in gateway; members pay by card through the active gateway.",
        "resolved_by": "expert@studio.test", "scope": "general"})
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    r2 = client.post("/query", json={"question": q}).json()
    assert any(s["kind"] == "expert" for s in r2["sources"])
    # resolving twice is a conflict
    assert client.post(f"/escalations/{eid}/resolve", json={"answer": "x" * 5, "resolved_by": "a"}).status_code == 409


def test_customer_specific_answer_is_not_indexed(client):
    r = client.post("/query", json={"question": "Why was Acme Plumbing charged twice in March?"}).json()
    client.post(f"/escalations/{r['escalation_id']}/resolve", json={
        "answer": "A duplicate webhook; refunded manually.", "resolved_by": "ops", "scope": "customer_specific"})
    r2 = client.post("/query", json={"question": "Why was Acme Plumbing charged twice in March?"}).json()
    assert not any(s["kind"] == "expert" for s in r2["sources"])


def test_validation_errors_have_consistent_shape(client):
    r = client.post("/query", json={"question": ""})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_request" and err["request_id"]


def test_auth_required_when_key_set(services):
    services.settings.api_key = "secret"
    app = create_app(services)
    with TestClient(app) as c:
        assert c.post("/query", json={"question": "refund"}).status_code == 401
        ok = c.post("/query", json={"question": "How do I refund a payment?"}, headers={"X-API-Key": "secret"})
        assert ok.status_code == 200
        assert c.get("/health").status_code == 200  # health stays open
    services.settings.api_key = None


def test_invalid_model_citation_is_reported_not_passed_through(services):
    asyncio.run(services.pipeline.run("incremental"))
    llm: FakeLLM = services.llm
    llm.handlers["submit_answer"] = lambda text: {"status": "answered", "answer": "Made up [S99]", "citations": [{"source_id": "S99"}]}
    app = create_app(services)
    with TestClient(app) as c:
        r = c.post("/query", json={"question": "How do I refund a payment?"}).json()
    assert r["status"] == "error" and "S99" in r["error"]


def test_ingestion_status_and_runs(client):
    s = client.get("/ingestion/status").json()
    assert s["pages"]["indexed"] == 4 and s["last_run"]["status"] == "completed"
    runs = client.get("/ingestion/runs").json()["runs"]
    assert runs[0]["counts"]["new"] == 4


def test_article_inspection(client):
    a = client.get("/articles/5000674165").json()
    assert a["title"] == "How Refunds work" and len(a["images"]) == 2
    assert a["images"][0]["description"]
