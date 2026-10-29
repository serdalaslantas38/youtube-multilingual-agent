"""Deterministic publishing policy engine.

Evaluates whether a localized asset is allowed to be published to YouTube.
This is a pure function with zero I/O so every policy rule can be exhaustively
tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.app_config import PublishingPolicyConfig, PublishMode


@dataclass(frozen=True, slots=True)
class PublishEvaluationInput:
    language: str
    youtube_publishing_enabled: bool
    validation_passed: bool
    review_passed: bool
    source_ambiguous: bool
    has_unreconciled_uncertain_attempt: bool
    target_already_exists_on_youtube: bool
    content_hash: str
    approved_content_hash: str | None = None
    explicit_overwrite_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PublishDecision:
    allowed: bool
    effective_mode: PublishMode
    reason_code: str
    message: str


def evaluate_publish_decision(
    policy: PublishingPolicyConfig,
    inp: PublishEvaluationInput,
) -> PublishDecision:
    """Enforce all hard gates and per-language publishing policies."""
    effective = policy.effective_mode(inp.language)

    # Gate 1: Environment-level kill switch
    if not inp.youtube_publishing_enabled:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="publishing_disabled_kill_switch",
            message="YOUTUBE_PUBLISHING_ENABLED is false; all YouTube writes are blocked.",
        )

    # Gate 2: Language disabled in policy
    if effective is PublishMode.DISABLED:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="language_disabled",
            message=f"Publishing is disabled for language {inp.language!r}.",
        )

    # Gate 3: Uncertain previous API outcome must be reconciled first
    if inp.has_unreconciled_uncertain_attempt:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="uncertain_previous_attempt",
            message="A previous publish attempt has an uncertain outcome; reconcile before retry.",
        )

    # Gate 4: Existing asset protection (never delete or overwrite without explicit authorization)
    if inp.target_already_exists_on_youtube and not inp.explicit_overwrite_authorized:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="target_already_exists",
            message=(
                f"Target asset for {inp.language!r} already exists on YouTube; "
                "skipping to prevent overwrite."
            ),
        )

    # Gate 5: Deterministic validation must pass
    if not inp.validation_passed:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="validation_failed",
            message="Deterministic validation failed.",
        )

    # Gate 6: Quality review & source ambiguity
    if inp.source_ambiguous or not inp.review_passed:
        return PublishDecision(
            allowed=False,
            effective_mode=effective,
            reason_code="quality_review_blocked",
            message="Quality review flagged issues or ambiguous source content.",
        )

    # Gate 7: Approval requirement (either configured or because global auto_publish_enabled=false)
    if effective is PublishMode.APPROVAL_REQUIRED:
        if not inp.approved_content_hash:
            return PublishDecision(
                allowed=False,
                effective_mode=effective,
                reason_code="approval_missing",
                message=f"Language {inp.language!r} requires explicit human approval.",
            )
        if inp.approved_content_hash != inp.content_hash:
            return PublishDecision(
                allowed=False,
                effective_mode=effective,
                reason_code="approval_invalidated_by_edit",
                message="Content was edited after approval; re-approval is required.",
            )

    return PublishDecision(
        allowed=True,
        effective_mode=effective,
        reason_code="allowed",
        message=f"Publishing allowed under mode {effective.value!r}.",
    )
