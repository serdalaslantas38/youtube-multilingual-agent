"""Exhaustive Stage 6 policy engine truth-table tests.

Verifies every combination of:
(configured_mode × global_auto_publish × kill_switch × uncertain_attempt ×
 existing_target × validation_passed × review_passed × source_ambiguous × approval_state)
= 1,152 truth-table rows.
"""

from __future__ import annotations

import itertools

from app.core.app_config import GlobalPolicy, PublishingPolicyConfig, PublishMode
from app.services.policy.engine import PublishEvaluationInput, evaluate_publish_decision

CONTENT_HASH = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
STALE_HASH = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


def _expected_decision(
    *,
    configured_mode: PublishMode,
    global_auto_publish: bool,
    kill_switch_enabled: bool,
    has_uncertain: bool,
    target_exists: bool,
    validation_passed: bool,
    review_passed: bool,
    source_ambiguous: bool,
    approved_hash: str | None,
) -> tuple[bool, str]:
    effective_mode = (
        PublishMode.APPROVAL_REQUIRED
        if (configured_mode is PublishMode.AUTO_AFTER_VALIDATION and not global_auto_publish)
        else configured_mode
    )

    if not kill_switch_enabled:
        return False, "publishing_disabled_kill_switch"
    if effective_mode is PublishMode.DISABLED:
        return False, "language_disabled"
    if has_uncertain:
        return False, "uncertain_previous_attempt"
    if target_exists:
        return False, "target_already_exists"
    if not validation_passed:
        return False, "validation_failed"
    if source_ambiguous or not review_passed:
        return False, "quality_review_blocked"
    if effective_mode is PublishMode.APPROVAL_REQUIRED:
        if not approved_hash:
            return False, "approval_missing"
        if approved_hash != CONTENT_HASH:
            return False, "approval_invalidated_by_edit"
    return True, "allowed"


def test_complete_policy_truth_table_all_1152_combinations() -> None:
    modes = (
        PublishMode.AUTO_AFTER_VALIDATION,
        PublishMode.APPROVAL_REQUIRED,
        PublishMode.DISABLED,
    )
    bools = (False, True)
    approval_states: tuple[str | None, ...] = (None, CONTENT_HASH, STALE_HASH)

    tested_rows = 0
    allowed_rows = 0

    for (
        configured_mode,
        global_auto,
        kill_switch,
        has_uncertain,
        target_exists,
        val_passed,
        rev_passed,
        src_ambiguous,
        approved_hash,
    ) in itertools.product(
        modes,
        bools,
        bools,
        bools,
        bools,
        bools,
        bools,
        bools,
        approval_states,
    ):
        policy = PublishingPolicyConfig(
            version=1,
            **{"global": GlobalPolicy(auto_publish_enabled=global_auto)},
            default_mode=PublishMode.APPROVAL_REQUIRED,
            languages={"en": configured_mode},
        )
        inp = PublishEvaluationInput(
            language="en",
            youtube_publishing_enabled=kill_switch,
            validation_passed=val_passed,
            review_passed=rev_passed,
            source_ambiguous=src_ambiguous,
            has_unreconciled_uncertain_attempt=has_uncertain,
            target_already_exists_on_youtube=target_exists,
            content_hash=CONTENT_HASH,
            approved_content_hash=approved_hash,
        )
        decision = evaluate_publish_decision(policy, inp)
        expected_allowed, expected_reason = _expected_decision(
            configured_mode=configured_mode,
            global_auto_publish=global_auto,
            kill_switch_enabled=kill_switch,
            has_uncertain=has_uncertain,
            target_exists=target_exists,
            validation_passed=val_passed,
            review_passed=rev_passed,
            source_ambiguous=src_ambiguous,
            approved_hash=approved_hash,
        )
        assert decision.allowed is expected_allowed
        assert decision.reason_code == expected_reason
        tested_rows += 1
        if decision.allowed:
            allowed_rows += 1

    assert tested_rows == 1152
    # Allowed only when kill_switch=True, has_uncertain=False, target_exists=False,
    # val_passed=True, rev_passed=True, src_ambiguous=False, and either:
    # - configured=auto & global_auto=True (all 3 approval states allowed -> 3 rows)
    # - configured=auto & global_auto=False & approved_hash=CONTENT_HASH (1 row)
    # - configured=approval & global_auto in (False, True) & approved_hash=CONTENT_HASH (2 rows)
    assert allowed_rows == 6
