"""Repository hygiene checks that protect secrets and project invariants."""

from __future__ import annotations

import re
from pathlib import Path

REQUIRED_PATHS = [
    "backend/app/api",
    "backend/app/agents",
    "backend/app/services",
    "backend/app/models",
    "backend/app/schemas",
    "backend/app/prompts",
    "backend/app/tasks",
    "backend/app/db",
    "backend/tests/unit",
    "backend/tests/integration",
    "backend/tests/contract",
    "backend/tests/e2e",
    "frontend",
    "config/languages.yaml",
    "config/publishing-policy.yaml",
    "docs",
    "AGENTS.md",
    "README.md",
    ".env.example",
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.yml",
]


def test_required_structure_exists(repo_root: Path) -> None:
    missing = [p for p in REQUIRED_PATHS if not (repo_root / p).exists()]
    assert not missing, f"missing: {missing}"


def test_gitignore_protects_secrets(repo_root: Path) -> None:
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "client_secret*.json", "secrets/", "token*.json"):
        assert pattern in gitignore, pattern
    assert "!.env.example" in gitignore


def test_dockerignore_protects_secrets(repo_root: Path) -> None:
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "client_secret*.json", "secrets/"):
        assert pattern in dockerignore, pattern


def test_env_example_contains_no_secret_values(repo_root: Path) -> None:
    secret_keys = {
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GEMINI_API_KEY",
        "ENCRYPTION_KEY",
        "WEBSUB_SECRET",
        "YOUTUBE_CHANNEL_ID",
    }
    for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() in secret_keys:
            assert value.strip() == "", f"{key} must be empty in .env.example"


def test_env_example_keeps_publishing_disabled(repo_root: Path) -> None:
    text = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^YOUTUBE_PUBLISHING_ENABLED=false$", text, re.MULTILINE)


def test_agents_md_contains_project_rules(repo_root: Path) -> None:
    text = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Never publish real YouTube content during automated tests." in text
    assert "Never report an operation as successful without verification." in text
