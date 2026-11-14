# YouTube Multilingual Publishing Agent

Automates the multilingual publishing work for a Turkish YouTube channel:
detect new uploads → prepare localized titles, descriptions and subtitles →
validate → review/approve → publish through the **official YouTube Data API**,
with per-language publishing policies.

> **Status: advanced single-channel application; not yet commercial multi-tenant SaaS.**
> The repository includes YouTube OAuth/discovery, localization, approvals, and
> guarded publishing with verification/reconciliation. Publishing remains gated by
> `YOUTUBE_PUBLISHING_ENABLED=false` and `auto_publish_enabled: false` by default.
> See [SaaS readiness](docs/saas-readiness.md) before deploying for multiple customers.

## Documentation
- [Architecture & risks](docs/architecture.md)
- [YouTube caption API investigation](docs/youtube-caption-api-investigation.md)
- [Implementation plan & acceptance criteria](docs/implementation-plan.md)
- [Manual Google Cloud / OAuth setup](docs/manual-setup.md)
- [Security model](docs/security.md)
- [SaaS readiness assessment and implementation sequence](docs/saas-readiness.md)
- [Architecture decisions](docs/decisions.md)
- [Project rules for agents](AGENTS.md)

## Repository layout
```
backend/app/
  api/            FastAPI routers
  agents/         LLM agents: metadata localizer, quality reviewer (data only)
  orchestration/  deterministic workflow state machine
  services/       youtube (typed API layer), translation, captions,
                  validation, policy, publishing, monitoring
  models/ db/     SQLAlchemy models, engine, Alembic
  schemas/        Pydantic schemas
  prompts/        versioned prompt templates
  tasks/          durable job queue and retry helpers
  core/           settings + YAML config loading
backend/scripts/poc/   caption-translation proof of concept (Stage 3)
backend/tests/{unit,integration,contract,e2e}/
config/languages.yaml, config/publishing-policy.yaml
frontend/        React + Vite dashboard
docs/
```

## Local development (macOS)
Requirements: Python ≥ 3.12, Docker Desktop (optional, for PostgreSQL).

```bash
python3 -m venv .venv            # skip if .venv already exists
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env             # then edit; never commit .env
make test                        # backend tests (no network, no credentials)
make lint typecheck              # lint + strict type checking
cd frontend && npm ci && npm run build
make db-up                       # optional: local PostgreSQL on 127.0.0.1:5432
```

SQLite (`sqlite+aiosqlite:///./data/app.db`) is the default local database;
PostgreSQL is used in production and can be used locally via docker compose.

## Current product boundary
- The current data model and API are single-tenant. Do not expose this deployment
  to unrelated customer accounts until organization ownership and authorization are
  implemented and verified.
- The existing database-backed queue provides durable enqueue/retry primitives; a
  separately deployed production worker is still required.

## Safety defaults
- Automated tests force `APP_ENV=test`; publishing cannot be enabled in that mode.
- Existing caption tracks and localizations are never overwritten or deleted.
- LLMs never call YouTube; all writes go through the deterministic policy engine
  and the publishing service, and are verified by reading back.

