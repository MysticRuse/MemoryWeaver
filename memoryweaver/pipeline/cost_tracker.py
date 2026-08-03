import json
import os
import threading
import time
from typing import Any, ClassVar

from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)


def _env_float(name: str, fallback: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


# Hard ceiling on recorded AI spend per calendar month. Set this to an amount
# you would be fine losing outright; enforce_ai_budget refuses billable routes
# once it is reached.
MONTHLY_BUDGET_USD = _env_float("MW_MONTHLY_BUDGET_USD", 25.0)


class CostTracker:
    """Itemized Cost & Escalation Rate Tracker for Photo/Video AI Features.
    Tracks monthly cost per feature, escalation rates (classical -> AI fallback),
    and per-user Magic generation counts to enforce cost control SLAs (<$0.50/user/mo).
    """

    # Feature cost rates per call ($ USD)
    FEATURE_RATES: ClassVar[dict[str, float]] = {
        "classification_caption_combined": 0.0004, # Gemini Flash combined call
        "classification_caption_combined_batch": 0.0002, # Same call via Batch API (~50% off, delayed turnaround)
        "vault_ocr_local": 0.0,                    # Native Apple Vision / Tesseract
        "vault_ocr_gemini_fallback": 0.00075,      # Gemini Vision fallback
        "magic_overlay_gen": 0.03,                 # Generative overlay/sticker
        "magic_overlay_cached": 0.0,               # Cached overlay template
        "video_scene_describe": 0.0015,            # Gemini Flash 5-8 frame diff
        "storage_optimizer": 0.0,                  # FFmpeg H.264 CRF ($0)
        "video_splitter": 0.0,                     # FFmpeg timestamp cut ($0)
        "watermark_remover_classical": 0.0,        # 20-frame variance inpaint ($0)
        "watermark_remover_gemini_fallback": 0.002 # Gemini Vision bbox escalation
    }

    def __init__(self, storage_dir: str = "local_storage/cost_logs"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self.log_file = os.path.join(self.storage_dir, "feature_costs.json")
        self._lock = threading.Lock()
        self._data = self._load_data()

    def _load_data(self) -> dict[str, Any]:
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("%s: best-effort step failed, continuing: %s", "cost_tracker", exc)
        return {
            "monthly_spend": {},       # YYYY-MM -> feature -> cost_usd
            "call_counts": {},         # YYYY-MM -> feature -> count
            "escalations": {},         # YYYY-MM -> feature -> count
            "magic_user_counts": {}    # session_id -> count
        }

    def _save_data(self):
        try:
            with open(self.log_file, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as ex:
            logger.warning(f"CostTracker save failed: {ex}")
    def record_feature_use(self, feature_name: str, session_id: str = "default", is_escalation: bool = False, custom_cost: float | None = None):
        month_key = time.strftime("%Y-%m")
        cost = custom_cost if custom_cost is not None else self.FEATURE_RATES.get(feature_name, 0.0)

        with self._lock:
            # 1. Update Monthly Spend
            month_spend = self._data["monthly_spend"].setdefault(month_key, {})
            month_spend[feature_name] = round(month_spend.get(feature_name, 0.0) + cost, 6)

            # 2. Update Call Counts
            month_calls = self._data["call_counts"].setdefault(month_key, {})
            month_calls[feature_name] = month_calls.get(feature_name, 0) + 1

            # 3. Track Escalations
            if is_escalation:
                month_esc = self._data["escalations"].setdefault(month_key, {})
                month_esc[feature_name] = month_esc.get(feature_name, 0) + 1
                logger.info(f"[COST ESCALATION LOG] Feature: {feature_name} | Session: {session_id} | Cost: ${cost:.5f}")
            # 4. Track Magic User Generations
            if feature_name.startswith("magic_overlay"):
                self._data["magic_user_counts"][session_id] = self._data["magic_user_counts"].get(session_id, 0) + 1

            self._save_data()

    def get_magic_user_count(self, session_id: str) -> int:
        with self._lock:
            return self._data["magic_user_counts"].get(session_id, 0)

    def get_month_total(self, month: str | None = None) -> float:
        """Total recorded spend for a month, in USD."""
        month_key = month or time.strftime("%Y-%m")
        with self._lock:
            return round(sum(self._data["monthly_spend"].get(month_key, {}).values()), 6)

    def over_budget(self) -> tuple[bool, float, float]:
        """Whether this month's recorded spend has hit the hard ceiling.

        Recording spend is not the same as capping it: before this, every
        billable call was metered into feature_costs.json and nothing ever
        read the total back. Returns (is_over, spent, ceiling).
        """
        ceiling = MONTHLY_BUDGET_USD
        spent = self.get_month_total()
        return spent >= ceiling, spent, ceiling

    def get_summary(self, month: str | None = None) -> dict[str, Any]:
        month_key = month or time.strftime("%Y-%m")
        with self._lock:
            spend = self._data["monthly_spend"].get(month_key, {})
            calls = self._data["call_counts"].get(month_key, {})
            escalations = self._data["escalations"].get(month_key, {})

            total_spend = sum(spend.values())

            # Escalation Rates
            escalation_rates = {}
            for feat, esc_count in escalations.items():
                total_calls = calls.get(feat, 1)
                escalation_rates[feat] = round((esc_count / max(total_calls, 1)) * 100.0, 2)

            return {
                "month": month_key,
                "total_spend_usd": round(total_spend, 4),
                "feature_spend_usd": spend,
                "feature_call_counts": calls,
                "feature_escalation_counts": escalations,
                "feature_escalation_rates_pct": escalation_rates
            }

_global_cost_tracker = CostTracker()

def get_cost_tracker() -> CostTracker:
    return _global_cost_tracker
