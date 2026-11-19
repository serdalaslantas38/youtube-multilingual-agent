# SaaS readiness assessment and implementation sequence

## Current status

This repository is a mature localization and publishing application being
prepared for multi-customer SaaS. Phase 1 added organization ownership and a
legacy-workspace backfill. Phase 2 adds verified identity, role checks, and
request-scoped tenant access while preserving the existing YouTube workflows.

### Existing capabilities confirmed in the source

- FastAPI API and React 19 / TypeScript / Vite dashboard.
- SQLAlchemy 2.x models and Alembic initial migration; SQLite development and
  PostgreSQL production configuration.
- Google OAuth for YouTube channel authorization, PKCE/state handling, and encrypted
  refresh-token persistence.
- Gemini translation provider abstraction, metadata localization, caption parsing
  and translation, deterministic validation, and quality review.
- Hash-bound approvals, policy settings, audit events, publish idempotency,
  read-back verification, and uncertain-outcome reconciliation.
- A database-backed idempotent job enqueue/retry helper.

### Remaining SaaS blockers

1. **Organization lifecycle is incomplete:** users, organizations, memberships,
   role permissions, and workspace switching now exist. Invitations, self-service
   workspace provisioning, and account recovery are not implemented.
2. **Worker runtime is incomplete:** durable job persistence and retry helpers exist,
   but the repository does not yet provide a complete continuously running worker
   service and operational scheduler.
3. **No billing or usage enforcement:** subscriptions, plan entitlements, usage
   metering, payment webhooks, and quota enforcement are not implemented.
4. **No SaaS customer onboarding:** the dashboard is an operator workflow rather
   than a self-service signup and workspace product.
5. **Production hardening remains:** deploy-time secret/KMS integration, session
   lifecycle operations, monitoring, backups, and incident runbooks need to be
   completed and exercised. API sessions now use CSRF tokens and tenant-aware audit
   events are scoped to the active organization.

## Required architecture

- Keep the existing FastAPI + SQLAlchemy modular monolith and React client.
- Use PostgreSQL as the production source of truth.
- Add `users`, `organizations`, and `organization_members` with explicit roles.
- Add `organization_id` ownership to every customer-owned root record, initially
  channel connections, videos, jobs, quota records, audit events, and policy data.
  Child records should inherit ownership through their parent and be accessed only
  through tenant-scoped parent lookups.
- Replace email-only login with a verified OIDC identity flow. Validate issuer,
  audience, signature, expiry, nonce/state, and verified email server-side; issue a
  short-lived server-managed session. Never trust a client-supplied email header
  except when it is injected by a separately verified IAP boundary.
- Resolve the active organization from authenticated membership on the server.
  Never accept `organization_id` as authorization evidence from request payloads.
- Add role checks for owner, admin, editor, and viewer. Publishing and billing
  configuration require elevated roles and existing approval/publishing safeguards.
- Keep YouTube OAuth distinct from SaaS login. Encrypt refresh tokens with a managed
  key in production and associate each connected channel with its organization.
- Keep the database-backed job queue initially; deploy a separate worker process
  against the same PostgreSQL database. Add lease claiming, bounded retries,
  concurrency limits, and organization-aware usage accounting.
- Add Stripe only after organization identity, entitlements, and usage accounting
  are reliable. Verify webhook signatures and make billing event handling idempotent.

## Safe implementation sequence

### Phase 1 — tenant schema and legacy migration

- Add user, organization, membership, and subscription/plan primitives.
- Add a migration that creates a clearly named legacy workspace and backfills all
  existing channel/video/job/audit data into it before making ownership non-null.
- Preserve existing global YouTube identifiers only if the product intentionally
  prevents the same YouTube channel from being connected to multiple customer
  workspaces; otherwise change uniqueness to `(organization_id, youtube_channel_id)`
  and define how a channel can be shared or transferred.
- Current Phase 2 keeps channel and video identifiers globally unique, so one
  YouTube channel belongs to one organization. A duplicate channel connection is
  rejected; sharing or transferring a channel needs a separate ownership design.
- Add migration upgrade/downgrade tests on SQLite and PostgreSQL.
- Do not deploy until tenant ownership is complete and verified.

### Phase 2 — verified identity and authorization

- Introduce OIDC login and server-side sessions.
- Add membership/role dependencies to every customer API route.
- Scope every root query and mutation by the authenticated organization.
- Add adversarial tests proving one organization cannot read, edit, approve, publish,
  reconcile, or inspect another organization's records by guessing IDs.
- Remove the email-only login path from production; retain a clearly isolated local
  development identity only under `APP_ENV=development`.

Phase 2 implementation adds Google OIDC state/nonce/PKCE validation, revocable
server-side sessions with CSRF checks, membership-verified organization selection,
owner/admin/editor/viewer route dependencies, and request-local organization scope.
The email-only login and operator/IAP header paths have been removed. The initial
legacy workspace can still bootstrap allowlisted users; newly provisioned
organizations can connect a channel without the legacy `YOUTUBE_CHANNEL_ID` pin.
Channel/video IDs remain globally unique, so channels cannot be shared across
organizations in this design. Workspace invitation and self-service creation remain
customer onboarding work for Phase 5.

### Phase 3 — worker and operational isolation

- Implement a separate worker entry point and deployment configuration.
- Include organization ownership in job payload validation and usage attribution.
- Add per-organization concurrency, retry, and rate limits.
- Verify crash recovery and that one tenant's workload cannot starve all others.

### Phase 4 — plans, usage, and billing

- Define measurable billable units (for example translated minutes, metadata jobs,
  or published language assets) before choosing plan limits.
- Add append-only usage events and idempotent aggregation.
- Enforce entitlements in the API and worker, not only in the frontend.
- Add Stripe checkout/customer portal/webhooks after the metering model is tested.

### Phase 5 — customer-facing product and production rollout

- Add self-service signup, workspace setup, channel connection, onboarding, and
  clear review/publishing controls.
- Deploy API and worker on Cloud Run, PostgreSQL on Cloud SQL, secrets in Secret
  Manager, and encryption keys through Cloud KMS.
- Add backups, restore drills, structured logs, metrics, alerting, support tooling,
  privacy/data export/deletion flows, and an incident runbook.
- Launch to a small private beta only after security review and cross-tenant tests.

## Release gate

The application must not be marketed or exposed as multi-tenant SaaS until all
customer-owned data is tenant-scoped, every API path has authorization tests, the
legacy migration is rehearsed against a copy of production data, the worker runs
independently of a developer machine, and billing/entitlements are enforced
server-side. A successful frontend build or unit-test run alone is not a SaaS
security sign-off.
