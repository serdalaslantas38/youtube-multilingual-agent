## Project rules

- Never publish real YouTube content during automated tests.
- Keep YouTube API operations behind a typed service layer.
- Use deterministic policy checks for approval and publishing.
- Never expose OAuth tokens or API secrets to the frontend or LLM.
- Preserve unrelated video metadata when updating localizations.
- Validate every translated caption file before uploading.
- Make background jobs resumable and idempotent.
- Require tests for every publishing or approval policy change.
- Do not implement browser automation as a fallback until the
  official API route has been tested and its limitations documented.
- Never report an operation as successful without verification.


## Working notes for agents

- Read `docs/architecture.md` and `docs/implementation-plan.md` before changing code;
  work only on the current stage.
- Configuration lives in `config/*.yaml` (validated by `backend/app/core/app_config.py`)
  and environment settings (`backend/app/core/settings.py`). Do not hardcode
  language lists, policies, model names, or paths.
- Only `backend/app/services/youtube/` may call the YouTube API. Only
  `backend/app/services/publishing/` may perform YouTube writes, and only after
  a `PublishDecision` from the policy engine allows it.
- LLM agents (`backend/app/agents/`) return schema-validated data; they have no
  tools and never receive tokens or secrets.
- All I/O is async; wrap synchronous Google client calls in `asyncio.to_thread`.
- Commands: `make test`, `make lint`, `make typecheck`, `make check`.
- Never read, print, move, or commit files matching `client_secret*.json` or `.env`.

## Codex workflow and SaaS implementation rules

- Treat this repository as an existing production-oriented application, not a greenfield rewrite.
- Before each task, inspect the current branch, working tree, relevant code, tests, and the current stage in `docs/saas-readiness.md`. Also read `docs/architecture.md` and `docs/implementation-plan.md` when present.
- Work in small, reviewable steps. Do not implement multiple SaaS phases in one task. Do not merge pull requests or push directly to `master`.
- Start each implementation task by summarizing the plan, affected modules, migration impact, security risks, and tests. Ask before destructive operations or changes to production infrastructure.
- Preserve the existing YouTube localization, caption translation, approval, publishing, reconciliation, and durable-queue behavior. Extend these flows; do not replace them without an explicit approved design.
- Multi-tenancy is the first productization priority. Every customer-owned record and query must be scoped to the authenticated organization. Never trust an organization ID supplied by the browser without verifying membership and role on the server.
- Keep application identity/authentication (users, organizations, memberships, roles, OIDC) separate from YouTube OAuth credentials and channel authorization.
- Design database migrations to be backward-compatible where practical. Preserve existing data, provide a safe backfill for legacy records, and test upgrade paths against a copy/fixture of the existing schema.
- Add negative authorization tests, especially cross-organization read, update, delete, job access, and channel access attempts. Default to deny.
- Do not introduce Stripe billing, paid-plan enforcement, or a frontend redesign before tenant isolation and authentication foundations are implemented and tested.
- Keep background processing reliable and idempotent. Do not claim the production worker is operational unless its deployment and health checks are verified.
- Never weaken tests, lint, type checking, authorization, or publishing safeguards to make CI pass. Report missing dependencies, skipped tests, and environment limitations explicitly.
- Before finishing, run the narrowest relevant tests, then broader checks when feasible; inspect `git diff` for accidental secrets, unrelated edits, and generated files. Report exact commands and outcomes.
- Do not access, print, commit, or modify secrets, credentials, OAuth client files, `.env` files, production data, or live YouTube content.
