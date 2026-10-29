# Architecture decision records

## ADR-001 — Modular monolith
**Decision:** One Python package (`backend/app`), one image, `api` + `worker` roles.
**Why:** Single channel, low volume, one developer. Microservices would add
deployment, auth and consistency cost with no benefit. Module boundaries are
enforced by convention, code review and tests (see architecture.md §2).

## ADR-002 — PostgreSQL-backed durable job queue instead of Celery + Redis
**Decision:** Jobs live in a `job` table (unique dedupe key, `next_run_at`,
attempts, lock lease) claimed with `FOR UPDATE SKIP LOCKED`. Interface
`JobQueue` allows swapping to Cloud Tasks later.
**Why:** Same transactional store as the workflow state ⇒ "state change + enqueue"
is atomic (no lost or phantom jobs). Removes a Redis/Memorystore dependency
(cost + ops) in Google Cloud. Throughput needs are tiny (a few videos per day).
**Consequence:** `celery` and `redis` were removed from the initial dependencies.
SQLite (local) supports the same table with a single worker; PostgreSQL is used
for multi-worker and production.

## ADR-003 — Official API first; native `tlang` before LLM; no browser automation
**Decision:** Caption translation providers are tried in the order configured in
`publishing-policy.yaml` (`youtube_native`, then `llm`). Browser automation is
not implemented unless the PoC documents an unmet requirement.
**Why:** AGENTS.md and YouTube Terms of Service; automation of Studio is brittle
and risks account security.

## ADR-004 — Deterministic orchestrator and policy engine; LLMs produce data only
**Decision:** Orchestrator = state machine; policy = pure function. Agents return
schema-validated JSON and have no tools.
**Why:** Testability, resumability, and immunity to prompt injection.

## ADR-005 — Approvals are bound to content hashes
**Decision:** An approval stores the SHA-256 of the exact content (normalized
title/description or caption file bytes). Publishing requires a match.
**Why:** Editing after approval automatically invalidates the approval without
extra bookkeeping.

## ADR-006 — Async I/O with sync Google client in threads
**Decision:** FastAPI + async SQLAlchemy. `google-api-python-client` is
synchronous, so the YouTube service layer wraps calls with `asyncio.to_thread`.
**Why:** No blocking in the event loop while still using the official client.

## ADR-007 — React + Vite SPA dashboard
**Decision:** See `frontend/README.md`. The backend is the only holder of secrets.

## ADR-008 — Python version
`requires-python >=3.12`. Docker uses `python:3.12-slim`. The existing local venv
is Python 3.14, which is compatible; CI (later) should test 3.12 and the latest.

