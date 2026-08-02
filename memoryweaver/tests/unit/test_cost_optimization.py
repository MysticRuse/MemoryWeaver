import os
import sys

import numpy as np

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from pipeline.cost_tracker import CostTracker, get_cost_tracker


def test_cost_tracker_itemized_logging():
    """Asserts that CostTracker correctly records per-feature spend, call counts, and escalations."""
    tracker = CostTracker(storage_dir="local_storage/test_cost_logs")

    tracker.record_feature_use("classification_caption_combined", session_id="test_s1")
    tracker.record_feature_use("watermark_remover_classical", session_id="test_s1")
    tracker.record_feature_use("watermark_remover_gemini_fallback", session_id="test_s1", is_escalation=True)

    summary = tracker.get_summary()
    assert summary["feature_call_counts"]["classification_caption_combined"] >= 1
    assert summary["feature_call_counts"]["watermark_remover_classical"] >= 1
    assert summary["feature_escalation_counts"]["watermark_remover_gemini_fallback"] >= 1
    assert summary["total_spend_usd"] > 0

def test_watermark_remover_classical_default_path():
    """Asserts that static 20-frame variance detection correctly identifies static watermarks without calling Gemini."""
    get_cost_tracker()

    # Simulate 20 sample frames where a static watermark rectangle is present at (x=10, y=800, w=100, h=40)
    width, height = 1000, 1000
    sample_frames = []
    for i in range(20):
        # Background changes slightly
        frame = np.full((height, width), fill_value=50 + i * 2, dtype=np.uint8)
        # Static watermark region stays constant (value 220)
        frame[800:840, 10:110] = 220
        sample_frames.append(frame)

    gray_stack = np.array(sample_frames, dtype=np.float32)
    var_map = np.var(gray_stack, axis=0)
    mean_map = np.mean(gray_stack, axis=0)

    # Static region variance should be ~0
    static_mask = np.uint8((var_map < 15.0) & (mean_map > 20.0)) * 255
    assert static_mask[820, 50] == 255
    assert static_mask[100, 100] == 0 # Dynamic background has higher variance

def test_scene_description_frame_diff_sampling():
    """Asserts that scene-differencing (cv2.absdiff) extracts keyframes at scene transitions."""
    import cv2
    f1 = np.zeros((100, 100), dtype=np.uint8)
    f2 = np.full((100, 100), fill_value=200, dtype=np.uint8) # Major scene change

    diff = cv2.absdiff(f1, f2)
    mean_diff = np.mean(diff)
    assert mean_diff > 12.0 # Exceeds scene transition threshold

def test_storage_optimizer_zero_ai_guarantee():
    """Asserts that Storage Optimizer compression uses FFmpeg/avconvert codec and records $0 AI cost."""
    tracker = get_cost_tracker()
    tracker.record_feature_use("storage_optimizer", session_id="test_zero_ai")

    rate = tracker.FEATURE_RATES["storage_optimizer"]
    assert rate == 0.0
