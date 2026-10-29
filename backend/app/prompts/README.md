# Prompts

Versioned prompt templates for LLM agents (Stage 5).

Rules:
- One file per prompt, named `<agent>.<purpose>.v<N>.md` (e.g. `metadata_localizer.translate.v1.md`).
- Prompts receive **only** content to translate/review — never tokens, secrets,
  API keys, channel credentials, or tool/function access to YouTube.
- Agents must return JSON validated against a Pydantic schema in `app/schemas/`.
- Changing a prompt requires bumping the version; the version is stored with
  every generated translation for auditability.

