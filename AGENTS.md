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
