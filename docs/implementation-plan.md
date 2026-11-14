# Implementation plan

Each stage ends with a demo, passing `make check`, and a written report. No stage
starts until the previous stage's acceptance criteria are met. Items marked 🔐
need the owner's explicit approval or credentials.

## Stage 1 — Foundations ✅ (this stage)
**Deliverables:** architecture & risk docs, caption API investigation, ADRs, plan,
manual setup guide, repository skeleton, typed settings, validated YAML config,
Docker/compose, Makefile, secret hygiene, unit tests.
**Acceptance criteria**
- Required directory structure exists (tested).
- `config/*.yaml` load into strict Pydantic models; typos/unknown modes fail (tested).
- Global auto-publish is off; EN/RO configured auto, others approval; unknown languages default to approval (tested).
- `APP_ENV=test` cannot enable publishing (tested).
- Secrets are git- and docker-ignored; `.env.example` has no values (tested).

## Stage 2 — FastAPI foundation & persistence
**Deliverables:** `app/main.py` app factory, `/health` (liveness) and `/ready`
(DB + config) endpoints, structured logging, async SQLAlchemy engine/session,
ORM models (architecture.md §4), Alembic with initial migration, test DB fixtures.
**Acceptance criteria**
- `uvicorn app.main:app` starts; `/health` 200; `/ready` reports DB/config state.
- `alembic upgrade head` works on SQLite and on PostgreSQL (docker compose).
- Unique constraints exist for job dedupe keys and (video, language, kind).
- Integration tests run against SQLite and (optionally) PostgreSQL.

## Stage 3 — Caption translation PoC
**Deliverables:** typed `YouTubeClient` protocol + `FakeYouTubeClient`; contract
tests built from documented response shapes; PoC CLI (`backend/scripts/poc`).
- 3a (mocked): list/download/tlang/insert flows, error mapping (`403`, `404`, `409`, `couldNotConvert`), SRT/VTT validation.
- 3b 🔐 (read-only live): run on ONE dev video the owner designates.
- 3c 🔐 (write live): `captions.insert isDraft=true` on an **unlisted** dev video.
**Acceptance criteria**
- Assumptions A1–A12 in the investigation doc are each marked verified/refuted with evidence (sanitized logs, screenshots).
- Decision recorded: native-first vs LLM-first per track type.
- No writes to any non-dev video (guarded in code).

## Stage 4 — OAuth, discovery, caption inspection (read-only)
**Deliverables:** OAuth web flow (`youtube.force-ssl` only, `access_type=offline`,
`prompt=consent`, `state` + PKCE), encrypted refresh-token storage
(Fernet locally, KMS in prod), channel binding check (`channels.list mine=true`
must equal `YOUTUBE_CHANNEL_ID`), uploads-playlist listing, caption-track inspection,
quota ledger.
**Acceptance criteria**
- Tokens never appear in logs, API responses, or LLM inputs (tests assert).
- Connecting a different channel is rejected.
- Video list and caption tracks visible via API; no write scopes exercised.

## Stage 5 — Localization, fallback translation, validation
**Deliverables:** translation-provider registry (`youtube_native`, `gemini`, `fake`)
selected by config; metadata localization agent with placeholder protection
(URLs, @handles, #hashtags, numbers, names glossary); caption translator that
preserves cue count/timings; quality-review agent (structured issues with severity);
deterministic validators (SRT/VTT syntax, monotonic timestamps, overlaps, cue
count parity, empty cues, max line length, encoding, title ≤100 chars, description
≤5000 bytes, no `<`/`>`, language-code checks, existing-track detection).
**Acceptance criteria**
- Golden tests with Turkish fixtures for every validator rule.
- LLM outputs are schema-validated; invalid output ⇒ retry then `needs_attention`.
- Prompt version stored with every asset.

## Stage 6 — Dashboard & approval policies
**Deliverables:** policy engine (pure function + exhaustive tests); approval API
with hash binding and audit log; React/Vite dashboard (see frontend/README.md);
dashboard sign-in (verified Google OIDC, server-side sessions, and organization
roles); per-language settings stored in DB with audit.
**Acceptance criteria**
- Editing an approved asset invalidates approval (tested).
- Every row of the policy truth table tested (mode × global switch × validation × review × approval × uncertain state × kill switch).
- Creator can complete review of one video without technical help (usability walkthrough 🔐).

## Stage 7 — Controlled publishing & verification
**Deliverables:** publishing service (localizations merge-write, caption insert),
idempotency keys, read-back verification, reconciliation of uncertain outcomes,
publish history.
**Acceptance criteria**
- Contract tests prove unrelated snippet/status/localizations are preserved.
- Existing tracks are never overwritten/deleted (tested).
- 🔐 First real publish on a dev video with `YOUTUBE_PUBLISHING_ENABLED=true`, verified in Studio by the owner.
- Status shown as "published" only after verification succeeds.

## Stage 8 — Monitoring, background processing, retries
**Deliverables:** WebSub subscribe/renew, callback with HMAC + channel check,
durable job queue + worker, uploads-playlist reconciliation, awaiting-source backoff,
dedupe.
**Acceptance criteria**
- Webhook responds < 1 s and only enqueues.
- Duplicate notifications ⇒ one job (tested).
- Missed notification recovered by reconciliation (tested with fakes).
- Kill -9 of worker mid-job ⇒ job resumes without duplicate publish (tested).

## Stage 9 — Google Cloud deployment & operations
**Deliverables:** Cloud Run services, Cloud SQL, Secret Manager, KMS, Scheduler,
IAP/sign-in, CI (lint, type, tests), runbooks (token revoked, quota exceeded,
uncertain publish, rollback), alerting.
**Acceptance criteria**
- 🔐 Deploy to owner's project; end-to-end run on a dev video while the Mac is off.
- Alerts fire for failed/uncertain publishes and quota > 80 %.
- 🔐 Owner explicitly enables `auto_publish_enabled` (audited) — only after sign-off.

