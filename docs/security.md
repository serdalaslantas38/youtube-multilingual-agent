# Security model

## Secrets
- Never in source control: `.gitignore` and `.dockerignore` block `.env*`,
  `client_secret*.json`, tokens and keys (enforced by tests).
- Local: `.env` + client JSON stored outside the repo. Cloud: Secret Manager.
- Settings use `SecretStr`; secrets never appear in reprs/logs.
- Secrets and tokens are never sent to the frontend or included in LLM prompts.

## OAuth
- Google OAuth 2.0 web flow; we never see or store the Google password.
- Least privilege: `youtube.force-ssl` only (captions + localizations need it).
  `youtube.readonly` is insufficient for `captions.download`.
- `state` parameter + PKCE; redirect URI allowlist.
- Refresh tokens encrypted at rest (Fernet locally, Cloud KMS envelope encryption
  in production); access tokens kept in memory only.
- Channel binding: the authorized channel must equal `YOUTUBE_CHANNEL_ID`.

## Dashboard access
- Sign-in with Google (`openid email`) restricted to `DASHBOARD_ALLOWED_EMAILS`,
  or Cloud IAP. Session cookies: `HttpOnly`, `Secure`, `SameSite=Lax`, CSRF protection
  on state-changing requests.

## Webhook
- WebSub callback verifies `hub.challenge` only for subscriptions we initiated,
  validates `X-Hub-Signature` HMAC with `WEBSUB_SECRET`, checks channel id, parses
  XML with `defusedxml`, enforces body-size limit, and only enqueues a job; the
  worker re-confirms the video via the authenticated API.

## Publishing guardrails
- Environment kill switch `YOUTUBE_PUBLISHING_ENABLED` (default false; impossible to
  enable when `APP_ENV=test`).
- Global `auto_publish_enabled` (default false) + per-language modes.
- Deterministic policy engine; LLMs cannot trigger writes.
- Never delete/overwrite caption tracks or localizations without explicit per-item authorization.
- Read-merge-write + read-back verification; uncertain outcomes reconciled before retry.

## LLM safety
- Inputs are treated as untrusted (video text may contain injection attempts).
- No tools/function calling to external systems; strict JSON schema output.
- Protected tokens (URLs, handles, hashtags, numbers) verified deterministically after translation.

## Tests
- Automated tests run with `APP_ENV=test`, no real credentials, fake YouTube client.

