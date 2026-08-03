"""Guards the cost controls that only work if they stay wired in.

Each of these failed silently before: an output cap nothing passed, a token
governor that never saw a real token count, a spend ledger nothing read back,
and a bulk loop that fanned out past its own budget check. A unit test on the
class alone would have passed in every one of those cases, so these assert the
*call sites*, not just the mechanisms.
"""

import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.app_utils.ai_budget import max_ai_calls_per_run, response_tokens
from app.app_utils.genai_client import MAX_OUTPUT_TOKENS, text_config
from pipeline.cost_tracker import CostTracker
from pipeline.prompt_safety import CircuitBreaker

SOURCE_FILES = [
    "app/services/video_ops.py",
    "app/services/classification.py",
    "app/services/transforms.py",
    "app/fast_api_app.py",
    "app/routers/cleaner.py",
    "app/routers/video.py",
    "app/routers/photo.py",
]


def test_every_text_call_site_caps_output_tokens():
    """No PIPELINE_MODEL call may go out without an output-token ceiling.

    An uncapped call is the one that turns a bad prompt or a runaway thinking
    loop into an open-ended bill.
    """
    uncapped = []
    for rel in SOURCE_FILES:
        src = (PROJECT_ROOT / rel).read_text()
        for match in re.finditer(r"model=PIPELINE_MODEL,(?! config=text_config\(\),)", src):
            line = src[: match.start()].count("\n") + 1
            uncapped.append(f"{rel}:{line}")
    assert not uncapped, f"generate_content without an output cap: {uncapped}"


def test_text_config_applies_the_cap_and_allows_overrides():
    assert text_config().max_output_tokens == MAX_OUTPUT_TOKENS
    assert text_config(max_output_tokens=64).max_output_tokens == 64
    assert text_config(temperature=0.1).temperature == pytest.approx(0.1)


def test_client_bounds_its_retries():
    """Unbounded retries re-bill on every attempt; the ceiling must be explicit."""
    from app.app_utils import genai_client

    assert genai_client.RETRY_ATTEMPTS > 0
    src = (PROJECT_ROOT / "app/app_utils/genai_client.py").read_text()
    assert "HttpRetryOptions" in src, "client must pass explicit retry options"
    assert "attempts=RETRY_ATTEMPTS" in src


def test_actual_usage_replaces_the_reserved_estimate():
    """The hourly token ceiling has to see real usage, not the flat reservation."""
    breaker = CircuitBreaker(max_requests_per_min=100, max_tokens_per_hour=10_000)

    allowed, _ = breaker.check_budget("s1", estimated_tokens=500)
    assert allowed
    breaker.record_actual_usage("s1", 9_800)

    # The single large call must now exhaust the hourly budget, which it could
    # not do while the breaker still believed it had cost 500 tokens.
    allowed, message = breaker.check_budget("s1", estimated_tokens=500)
    assert not allowed
    assert "token budget" in message


def test_recorded_usage_does_not_double_count():
    breaker = CircuitBreaker(max_requests_per_min=100, max_tokens_per_hour=10_000)
    breaker.check_budget("s2", estimated_tokens=500)
    breaker.record_actual_usage("s2", 700)
    booked = sum(count for _, count in breaker._token_history["s2"])
    assert booked == 700, "reservation should be replaced, not added to"


def test_monthly_cap_is_read_back_not_just_recorded(tmp_path, monkeypatch):
    """Metering spend is not capping it: over_budget must reflect the ledger."""
    monkeypatch.setattr("pipeline.cost_tracker.MONTHLY_BUDGET_USD", 0.01)
    tracker = CostTracker(storage_dir=str(tmp_path / "cost_logs"))

    over, spent, ceiling = tracker.over_budget()
    assert not over and spent == 0.0 and ceiling == pytest.approx(0.01)

    for _ in range(40):  # 40 x $0.0004 = $0.016, past the ceiling
        tracker.record_feature_use("classification_caption_combined")

    over, spent, _ = tracker.over_budget()
    assert over and spent >= 0.01


def test_enforce_ai_budget_rejects_once_over_the_cap(monkeypatch):
    from fastapi import HTTPException

    from app import deps

    monkeypatch.setattr(
        deps, "get_cost_tracker", lambda: type("T", (), {"over_budget": lambda self: (True, 30.0, 25.0)})()
    )
    with pytest.raises(HTTPException) as excinfo:
        deps.enforce_ai_budget("s1")
    assert excinfo.value.status_code == 429
    assert "spend cap" in excinfo.value.detail


def test_bulk_analyze_bounds_its_fan_out():
    """analyze-all makes one billable call per image behind a single budget
    check, so it needs its own stopping point."""
    src = (PROJECT_ROOT / "app/routers/cleaner.py").read_text()
    assert "max_ai_calls_per_run()" in src
    assert "ai_calls_remaining -= 1" in src
    assert "over_budget()" in src, "spend ceiling must be re-checked inside the loop"


def test_max_ai_calls_per_run_is_configurable(monkeypatch):
    monkeypatch.setenv("MW_MAX_AI_CALLS_PER_RUN", "7")
    assert max_ai_calls_per_run() == 7
    monkeypatch.setenv("MW_MAX_AI_CALLS_PER_RUN", "not-a-number")
    assert max_ai_calls_per_run(fallback=200) == 200
    monkeypatch.delenv("MW_MAX_AI_CALLS_PER_RUN")
    assert max_ai_calls_per_run() == 200


def test_classification_paths_are_metered():
    """The highest-volume AI path was absent from the spend report entirely.

    The local OCR pass itself is metered inside the shared
    `run_local_ocr_credential_check` helper (both call sites use it), so this
    checks for that call rather than the "vault_ocr_local" literal.
    """
    classification_src = (PROJECT_ROOT / "app/services/classification.py").read_text()
    assert "vault_ocr_local" in classification_src, "shared OCR helper must meter its local pass"

    for rel in ("app/services/classification.py", "app/routers/cleaner.py"):
        src = (PROJECT_ROOT / rel).read_text()
        assert "run_local_ocr_credential_check(" in src, f"{rel}: local OCR pass not counted"
        assert "classification_caption_combined" in src, f"{rel}: Gemini call not metered"


def test_response_tokens_tolerates_missing_metadata():
    """Recording must never break a request that already succeeded."""
    assert response_tokens(None) == 0
    assert response_tokens(object()) == 0
    assert response_tokens(type("R", (), {"usage_metadata": None})()) == 0

    usage = type("U", (), {"total_token_count": 1234})()
    assert response_tokens(type("R", (), {"usage_metadata": usage})()) == 1234


def test_no_call_site_bypasses_the_shared_client():
    """A direct genai.Client(...) skips the retry bounds and the timeout."""
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path.name == "genai_client.py":
            continue
        if "genai.Client(" in path.read_text():
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, f"construct clients via get_gemini_client(): {offenders}"


def test_env_overrides_are_honoured_for_the_output_cap(monkeypatch):
    monkeypatch.setenv("MW_MAX_OUTPUT_TOKENS", "512")
    import importlib

    from app.app_utils import genai_client

    importlib.reload(genai_client)
    try:
        assert genai_client.MAX_OUTPUT_TOKENS == 512
    finally:
        monkeypatch.delenv("MW_MAX_OUTPUT_TOKENS")
        importlib.reload(genai_client)
    assert genai_client.MAX_OUTPUT_TOKENS == 4096


def test_every_generate_content_call_is_metered():
    """Every billable call must feed the spend ledger, or the 429 guardrail
    is blind to whatever route skipped it.

    Counting instead of pairing each call site by hand: a `generate_content(`
    that outnumbers `track_ai_call(` in the same file means at least one
    response went unmetered. This under-counts a site that manually calls
    `cost_tracker.record_feature_use` instead, but every current call site
    uses `track_ai_call`, so equal-or-more is the right invariant.
    """
    unmetered = []
    for rel in SOURCE_FILES:
        src = (PROJECT_ROOT / rel).read_text()
        calls = len(re.findall(r"generate_content\(", src))
        tracked = len(re.findall(r"track_ai_call\(", src))
        if tracked < calls:
            unmetered.append(f"{rel}: {calls} generate_content vs {tracked} track_ai_call")
    assert not unmetered, f"call site(s) missing track_ai_call(): {unmetered}"


def test_no_test_writes_into_the_live_media_pool():
    """local_storage/uploads is the real library; a test once wrote 258 files there."""
    assert not os.path.exists(PROJECT_ROOT / "local_storage" / "test_cost_logs" / "uploads")
