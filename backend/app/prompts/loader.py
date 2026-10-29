"""Versioned prompt loader with template rendering and secret-leakage guardrails."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

_PROMPT_NAME_RE = re.compile(r"^(?P<agent>[a-z0-9_]+)\.(?P<purpose>[a-z0-9_]+)\.(?P<version>v\d+)$")
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*(?P<key>[a-zA-Z0-9_]+)\s*\}\}")

# Patterns that must never appear in rendered LLM prompts (AGENTS.md safety invariant)
_FORBIDDEN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_oauth_client_secret", re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}")),
    ("google_access_token", re.compile(r"ya29\.[A-Za-z0-9._-]{15,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("oauth_refresh_token", re.compile(r"1//[0-9A-Za-z_-]{20,}")),
)


class PromptSecurityError(ValueError):
    """Raised when a prompt template or its variables contain forbidden credentials/secrets."""


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    """Loaded versioned prompt template."""

    name: str
    agent: str
    purpose: str
    version: str
    template: str

    def render(self, variables: dict[str, str]) -> str:
        """Render ``{{var}}`` placeholders and verify no unresolved variables or secrets exist."""
        missing: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            key = match.group("key")
            if key not in variables:
                missing.append(key)
                return match.group(0)
            return str(variables[key])

        rendered = _TEMPLATE_VAR_RE.sub(_replace, self.template)
        if missing:
            missing_vars = sorted(set(missing))
            raise ValueError(
                f"Prompt {self.name!r} is missing required template variables: {missing_vars}"
            )
        assert_no_secrets_in_text(rendered)
        return rendered


def assert_no_secrets_in_text(text: str) -> None:
    """Ensure no OAuth tokens, client secrets, or API keys are ever passed into an LLM prompt."""
    for label, pattern in _FORBIDDEN_SECRET_PATTERNS:
        if pattern.search(text):
            raise PromptSecurityError(
                f"Refusing to build LLM prompt: detected potential {label} in prompt payload."
            )


def resolve_prompts_dir(prompts_dir: Path | None = None) -> Path:
    if prompts_dir is not None and prompts_dir.exists():
        return prompts_dir
    return Path(__file__).resolve().parent


def load_prompt_template(name: str, prompts_dir: Path | None = None) -> LoadedPrompt:
    """Load a versioned prompt file named ``<agent>.<purpose>.v<N>.md``."""
    match = _PROMPT_NAME_RE.match(name.strip())
    if not match:
        raise ValueError(
            f"Invalid prompt name {name!r}; expected '<agent>.<purpose>.v<N>' "
            "(e.g. 'metadata_localizer.translate.v1')."
        )
    base_dir = resolve_prompts_dir(prompts_dir)
    file_path = base_dir / f"{name}.md"
    if not file_path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {file_path}")

    template = file_path.read_text(encoding="utf-8")
    return LoadedPrompt(
        name=name,
        agent=match.group("agent"),
        purpose=match.group("purpose"),
        version=match.group("version"),
        template=template,
    )


async def aload_prompt_template(name: str, prompts_dir: Path | None = None) -> LoadedPrompt:
    """Async non-blocking wrapper around ``load_prompt_template``."""
    return await asyncio.to_thread(load_prompt_template, name, prompts_dir)
