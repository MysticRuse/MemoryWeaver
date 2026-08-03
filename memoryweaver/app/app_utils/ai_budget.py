"""One call site for "this billable AI call happened".

Two separate ledgers need updating after every Gemini call, and they were
drifting apart: CostTracker knew the per-feature dollar rates but was only
wired into the two video paths, and the CircuitBreaker booked a flat 500-token
estimate it never corrected. Recording both in one helper means a new call
site cannot remember one and forget the other.
"""

from __future__ import annotations

import os
from typing import Any

from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)


def max_ai_calls_per_run(fallback: int = 200) -> int:
    """Hard ceiling on billable calls a single bulk endpoint may fan out into.

    A per-request budget check cannot see how many model calls that request
    will make. Bulk endpoints read this and stop when they hit it, which is
    the "defined stopping point" a fan-out loop otherwise lacks.
    """
    try:
        value = int(os.environ.get("MW_MAX_AI_CALLS_PER_RUN", ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def response_tokens(response: Any) -> int:
    """Total tokens a Gemini response reports actually using, or 0 if absent."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0
    try:
        return int(getattr(usage, "total_token_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def track_ai_call(
    feature: str,
    session_id: str = "default",
    response: Any = None,
    is_escalation: bool = False,
) -> None:
    """Meters one billable call against the spend ledger and the rate governor.

    Never raises: a failure to record must not fail the user's request, since
    the work has already been paid for by the time this runs.
    """
    try:
        from pipeline.cost_tracker import get_cost_tracker

        get_cost_tracker().record_feature_use(
            feature, session_id=session_id, is_escalation=is_escalation
        )
    except Exception as exc:
        logger.warning("cost tracking failed for %s: %s", feature, exc)

    if response is None:
        return

    try:
        tokens = response_tokens(response)
        if tokens:
            from pipeline.prompt_safety import get_circuit_breaker

            get_circuit_breaker().record_actual_usage(session_id, tokens)
    except Exception as exc:
        logger.warning("usage tracking failed for %s: %s", feature, exc)
