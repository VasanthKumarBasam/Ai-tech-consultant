# Knowledge Brain API contract (v1)

Base URL `http://127.0.0.1:8001`. Interactive OpenAPI docs at `/docs`. All bodies are JSON.
If `KB_API_KEY` is set, write endpoints require the header `X-API-Key: <key>` (Tasks 2 and 3 send it automatically).
Every response carries `X-Request-ID` (send your own to correlate logs across services).

## POST /query — answer a question with citations

Request
```json
{
  "question": "How do I refund a member's payment?",   // 3-2000 chars, required
  "context": "Customer email subject: Refund ...",      // optional, used for retrieval only
  "top_k": 8,                                           // optional, 1-20
  "retrieval_mode": "hybrid",                           // optional: vector | keyword | hybrid
  "model": "claude-sonnet-5-5",                         // optional override (evals)
  "create_escalation": true,                            // open an expert task if not fully answered
  "requester": "inbox-agent"                            // calling system, for the audit trail
}
```

Response 200
```json
{
  "request_id": "a1b2c3d4e5f6",
  "status": "answered",                 // answered | partial | insufficient_evidence | error
  "answer": "Go to My Members » Search Members ... [S1]",
  "sources": [
    {"id": "S1", "cited": true, "title": "How Refunds work", "url": "https://support.brilliantdirectories.com/...",
     "section": "How to refund a payment", "kind": "text",            // text | image (screenshot) | expert
     "image_url": null, "similarity": 0.71, "snippet": "...", "supporting_quote": "...", "chunk_id": "..."}
  ],
  "images": [],                         // the subset of sources that are screenshots
  "needs_human": false,                 // true unless status == answered
  "escalation_id": null,                // set when an expert task was opened (or an open duplicate reused)
  "unsupported_points": [],             // parts of the question the sources don't cover
  "reason": null,
  "model": "claude-sonnet-5-5", "prompt_version": "answer-v2",
  "retrieval": {"mode": "hybrid", "top_similarity": 0.71, "min_similarity": 0.55, "candidates": 8},
  "usage": {"input_tokens": 2310, "output_tokens": 180, "cost_usd": 0.0064, "validation_retries": 0},
  "latency_ms": 2140,
  "error": null
}
```
Field meanings
- `status=answered` means every statement is backed by a cited source. `partial`: some of it is. `insufficient_evidence`: nothing usable was found, either because no chunk passed the similarity gate (no model call) or the model said so. `error`: answer generation failed; never treat as an answer.
- `sources[].cited` distinguishes evidence the answer used from other retrieved chunks.
- There is deliberately **no `confidence` number**: model self-confidence is not calibrated. `retrieval.top_similarity` is the raw best-match cosine, reported for transparency.

## Other endpoints
| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service status, article/chunk counts, embedder, model, open escalations, last run |
| POST | `/search` | Retrieval only (no model): `{"query", "top_k", "retrieval_mode"}` → ranked chunks |
| POST | `/ingestion/run` | Start a run: `{"mode": "incremental\|full\|rebuild", "limit"?: N, "urls"?: [...]}` → 202, 409 if one is running |
| GET | `/ingestion/status` | Current/last run, pages by status, recent failures |
| GET | `/ingestion/runs` | Run history with counts (new, changed, unchanged, removed, failed) |
| POST | `/ingestion/runs/{id}/rollback` | Re-activate the article versions that run replaced |
| GET | `/articles/{article_id}` | Normalised article: sections, images and their descriptions |
| GET | `/escalations?status=open` | Expert queue |
| POST | `/escalations` | Create one: `{"question", "reason", "context"?, "requester"?}` (deduplicated while open) |
| POST | `/escalations/{id}/resolve` | `{"answer", "resolved_by", "scope": "general\|customer_specific", "expires_at"?, "supporting_url"?}` |
| POST | `/escalations/{id}/dismiss` | Close without an answer |
| GET | `/stats` | Query counts, average latency, total cost |

## Errors
All errors share one shape, with the HTTP status set accordingly (401, 404, 409, 422, 500):
```json
{"error": {"code": "invalid_request", "message": [{"field": "question", "message": "String should have at least 3 characters"}], "request_id": "..."}}
```
Clients should treat 5xx and timeouts as "knowledge unavailable" (retry once, then degrade), and 4xx as a bug in the request (no retry).
