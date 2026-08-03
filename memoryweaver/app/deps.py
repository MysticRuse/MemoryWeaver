"""Shared FastAPI dependencies.

Two cross-cutting concerns every mutating or billable route depends on: the
admin token and the AI spend cap.

This module also used to hold per-session progress and log stores plus
in-flight run locks. Those existed for the curation pipeline's long-running
background jobs; the pipeline is gone and every remaining operation is either
synchronous or reports progress inline, so they were removed rather than left
as unread globals.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException

from pipeline.cost_tracker import get_cost_tracker
from pipeline.prompt_safety import get_circuit_breaker


# ------------------------------------------------------------------
# Access control (STRIDE: spoofing/tampering - see CONTEXT.md)
#
# Destructive or billable endpoints require a shared admin token when
# MW_ADMIN_TOKEN is set. When it is unset (local development on a trusted
# machine) the check is a no-op so the app stays frictionless. Deployments
# MUST set it. The token is accepted via the X-MW-Token header.
# ------------------------------------------------------------------
def require_admin_token(x_mw_token: str | None = Header(None), token: str | None = None):
    expected = os.environ.get("MW_ADMIN_TOKEN")
    if expected:
        # compare_digest: a plain != leaks the secret's length and prefix
        # through response timing.
        supplied = x_mw_token or token or ""
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid X-MW-Token.")


def enforce_ai_budget(session_id: str = "default"):
    """Rejects a billable AI request once a budget ceiling is reached.

    Two independent ceilings, because they catch different failures:

    * the monthly dollar cap (MW_MONTHLY_BUDGET_USD) is the kill switch - it
      stops spend that is legitimate per-request but has added up past what
      the project is willing to pay;
    * the CircuitBreaker's rate/token governor catches a *burst* - a loop, a
      bot, or a runaway retry - within the hour rather than at month end.

    A month's spend can be under the cap while an hour's velocity is clearly
    abnormal, so neither check subsumes the other.
    """
    over, spent, ceiling = get_cost_tracker().over_budget()
    if over:
        raise HTTPException(
            status_code=429,
            detail=(
                f"AI spend cap reached: ${spent:.2f} of ${ceiling:.2f} this month. "
                "Raise MW_MONTHLY_BUDGET_USD to continue."
            ),
        )

    allowed, message = get_circuit_breaker().check_budget(session_id)
    if not allowed:
        raise HTTPException(status_code=429, detail=message)
