"""Failure-handling guarantees for the curation pipeline and its helpers.

What these lock in:
  * A failed Gemini call aborts the run WITHOUT touching an event's existing
    highlights/journal/story (a failed re-run used to overwrite a finished
    journal with an empty one and still report "complete").
  * Failed API calls are never cached as if they were real results (a cached
    fake quarantine / placeholder score / fake embedding stuck to the photo).
  * Cache entries poisoned by older failed runs are recomputed.
  * The MCP "missed moments" tool counts only moments that are in the journal.
  * The server-side folder picker needs the admin token, is unavailable on
    cloud deployments, and only copies photo files.
  * Capture times survive from Curate into the manifest and Proceed, so the
    journal comes out in chronological order with real dates.

Every Gemini call is stubbed and all storage is redirected to tmp_path, so
these tests are free, offline, and never touch real events.
"""

import json
import os
import types

import pytest
from PIL import Image

from pipeline import orchestrator as orch

FAIL_REASON = "Moderation batch request failed or skipped for this file."


# --------------------------------------------------------------------------
# Fakes / stubs
# --------------------------------------------------------------------------

class _FakeStorage:
    base = ""

    def __init__(self, session_id="t"):
        self.local_base = _FakeStorage.base


class _FakeSessionStore:
    def get_session(self, session_id):
        return None


class _FakeMemory:
    def __init__(self, session_id):
        pass

    def get_contributor_profile(self, cid):
        return None

    def upsert_contributor(self, **kwargs):
        pass

    def get_trip_context(self):
        return {}


def _good_mod(batch):
    return [
        {"usable": True, "appropriate": True, "sharp": True, "real_photo": True, "reason": "ok"}
        for _ in batch
    ]


def _failing_mod(batch):
    return [
        {"usable": False, "appropriate": False, "sharp": False, "real_photo": False,
         "reason": FAIL_REASON, "transient": True, "api_error": True}
        for _ in batch
    ]


def _real_quarantine_mod(batch):
    return [
        {"usable": False, "appropriate": True, "sharp": False, "real_photo": True, "reason": "blurry"}
        for _ in batch
    ]


def _good_score(batch):
    return [
        {"score": 7.0, "sharpness": 7.0, "composition": 7.0, "uniqueness": 7.0, "human_presence": 7.0,
         "scene_label": f"scene {i}", "caption": f"caption {i}"}
        for i, _ in enumerate(batch)
    ]


def _failing_score(batch):
    return [
        {"score": 5.0, "sharpness": 5.0, "composition": 5.0, "uniqueness": 5.0, "human_presence": 5.0,
         "scene_label": "unknown", "caption": "Exploring the sights.", "transient": True, "api_error": True}
        for _ in batch
    ]


_ONE_HOT = {}


def _good_embed(path):
    # one-hot vectors: every photo is distinct, so dedup never merges them
    idx = _ONE_HOT.setdefault(path, len(_ONE_HOT))
    vec = [0.0] * 16
    vec[idx % 16] = 1.0
    return vec, False


def _fallback_embed(path):
    return [0.1] * 128, True


KEEP_JOURNAL = [{"moment": "keep me", "entry": "a finished entry", "photos": ["x.jpg"], "date": "d"}]
KEEP_HIGHLIGHTS = [{"filename": "x.jpg", "scene_label": "keep me"}]
KEEP_STORY = "A finished story that must survive failures."


@pytest.fixture
def event(tmp_path, monkeypatch):
    """A fake event with 3 photos and a known-good finished set of artifacts."""
    base = tmp_path / "event"
    uploads = base / "uploads"
    art = base / "artefacts"
    uploads.mkdir(parents=True)
    art.mkdir(parents=True)
    for name in ("aa_1.jpg", "bb_2.jpg", "cc_3.jpg"):
        Image.new("RGB", (8, 8), (10, 20, 30)).save(uploads / name, "JPEG")
    (art / "journal.json").write_text(json.dumps(KEEP_JOURNAL))
    (art / "highlights.json").write_text(json.dumps(KEEP_HIGHLIGHTS))
    (art / "story.txt").write_text(KEEP_STORY)

    _FakeStorage.base = str(base)
    _ONE_HOT.clear()
    monkeypatch.setattr(orch, "StorageHelper", _FakeStorage)
    monkeypatch.setattr(orch, "MemoryBankStore", _FakeMemory)
    monkeypatch.setattr("app.app_utils.sessions.SessionStore", _FakeSessionStore)
    monkeypatch.setattr(orch, "run_vision_moderation_batch", _good_mod)
    monkeypatch.setattr(orch, "score_photos_as_judge_batch", _good_score)
    monkeypatch.setattr(orch, "get_image_embedding_checked", _good_embed)

    def finished_artifacts_intact():
        return (
            json.loads((art / "journal.json").read_text()) == KEEP_JOURNAL
            and json.loads((art / "highlights.json").read_text()) == KEEP_HIGHLIGHTS
            and (art / "story.txt").read_text() == KEEP_STORY
        )

    return types.SimpleNamespace(
        art=art, uploads=uploads, intact=finished_artifacts_intact,
        cache=lambda: json.loads((art / "curation_cache.json").read_text()),
        run=lambda **kw: orch.execute_trip_pipeline("unused", session_id="t", log=lambda m: None, **kw),
    )


# --------------------------------------------------------------------------
# Curate stage: failures abort cleanly
# --------------------------------------------------------------------------

def test_moderation_api_failure_aborts_without_touching_artifacts(event, monkeypatch):
    monkeypatch.setattr(orch, "run_vision_moderation_batch", _failing_mod)
    with pytest.raises(orch.PipelineAPIError, match="moderation failed"):
        event.run(stage="curate", limit=10)
    assert event.intact()
    # nothing was cached as a (fake) quarantine
    cache = event.cache() if (event.art / "curation_cache.json").exists() else {}
    assert not any("moderation" in v for v in cache.values())


def test_scoring_api_failure_aborts_but_keeps_earlier_work_cached(event, monkeypatch):
    monkeypatch.setattr(orch, "score_photos_as_judge_batch", _failing_score)
    with pytest.raises(orch.PipelineAPIError, match="scoring failed"):
        event.run(stage="curate", limit=10)
    assert event.intact()
    cache = event.cache()
    assert all("moderation" in v and "embedding" in v for v in cache.values()), "successful work should be cached"
    assert not any("scoring" in v for v in cache.values()), "placeholder scores must not be cached"


def test_all_photos_really_quarantined_raises_and_preserves_artifacts(event, monkeypatch):
    monkeypatch.setattr(orch, "run_vision_moderation_batch", _real_quarantine_mod)
    with pytest.raises(ValueError, match="No usable photos"):
        event.run(stage="curate", limit=10)
    assert event.intact()


def test_happy_path_curate_writes_highlights_and_clean_cache(event):
    stats = event.run(stage="curate", limit=10)
    assert stats["approved"] == 3 and stats["unique"] == 3
    highlights = json.loads((event.art / "highlights.json").read_text())
    assert len(highlights) == 3
    # curate resets the narrative until the user approves and presses Proceed
    assert json.loads((event.art / "journal.json").read_text()) == []
    for entry in event.cache().values():
        assert set(entry) >= {"moderation", "embedding", "scoring"}
        assert "transient" not in entry["scoring"] and "api_error" not in entry["scoring"]


def test_fallback_embeddings_are_used_but_never_cached(event, monkeypatch):
    monkeypatch.setattr(orch, "get_image_embedding_checked", _fallback_embed)
    event.run(stage="curate", limit=10)  # degraded dedup, but the run still succeeds
    assert not any("embedding" in v for v in event.cache().values())


def test_cache_entries_poisoned_by_earlier_failures_are_recomputed(event, monkeypatch):
    poisoned = {
        "aa_1.jpg": {"moderation": {"usable": False, "reason": FAIL_REASON}},
        "bb_2.jpg": {
            "moderation": {"usable": True, "reason": "ok"},
            "embedding": [1.0] * 16,
            "scoring": {"score": 5.0, "scene_label": "unknown", "caption": "Exploring the sights.",
                        "sharpness": 5.0, "composition": 5.0, "uniqueness": 5.0, "human_presence": 5.0,
                        "filename": "bb_2.jpg", "contributor_id": "bb"},
        },
    }
    (event.art / "curation_cache.json").write_text(json.dumps(poisoned))

    moderated, scored = [], []

    def spy_mod(batch):
        moderated.extend(i["filename"] for i in batch)
        return _good_mod(batch)

    def spy_score(batch):
        scored.extend(i["filename"] for i in batch)
        return _good_score(batch)

    monkeypatch.setattr(orch, "run_vision_moderation_batch", spy_mod)
    monkeypatch.setattr(orch, "score_photos_as_judge_batch", spy_score)
    event.run(stage="curate", limit=10)

    assert "aa_1.jpg" in moderated, "poisoned fake-quarantine must be re-moderated"
    assert "bb_2.jpg" in scored, "placeholder score must be re-scored"
    assert event.cache()["aa_1.jpg"]["moderation"]["usable"] is True
    assert event.cache()["bb_2.jpg"]["scoring"]["scene_label"] != "unknown"


# --------------------------------------------------------------------------
# Narrate stage: failures abort cleanly
# --------------------------------------------------------------------------

@pytest.fixture
def narrate_ready(event):
    manifest = [
        {"filename": f"{c}_{i}.jpg", "score": 7.0, "sharpness": 7.0, "composition": 7.0,
         "uniqueness": 7.0, "human_presence": 7.0, "scene_label": f"scene {i}",
         "caption": f"caption {i}", "contributor_id": c}
        for i, c in ((1, "aa"), (2, "bb"), (3, "cc"))
    ]
    (event.art / "manifest.json").write_text(json.dumps(manifest))
    (event.art / "highlights.json").write_text(json.dumps(manifest))
    return event


def test_narration_api_failure_aborts_without_touching_journal_or_story(narrate_ready, monkeypatch):
    def boom(moments):
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(orch, "generate_all_moments_journal_strict", boom)
    with pytest.raises(orch.PipelineAPIError, match="narration failed"):
        narrate_ready.run(stage="narrate", limit=10)
    assert json.loads((narrate_ready.art / "journal.json").read_text()) == KEEP_JOURNAL
    assert (narrate_ready.art / "story.txt").read_text() == KEEP_STORY


def test_empty_narration_result_is_treated_as_failure(narrate_ready, monkeypatch):
    monkeypatch.setattr(orch, "generate_all_moments_journal_strict", lambda moments: {})
    with pytest.raises(orch.PipelineAPIError, match="no usable entries"):
        narrate_ready.run(stage="narrate", limit=10)
    assert json.loads((narrate_ready.art / "journal.json").read_text()) == KEEP_JOURNAL


def test_story_failure_aborts_without_touching_artifacts(narrate_ready, monkeypatch):
    monkeypatch.setattr(
        orch, "generate_all_moments_journal_strict",
        lambda moments: {m["moment"]: "an entry" for m in moments},
    )

    def boom(**kwargs):
        raise RuntimeError("429 quota")

    monkeypatch.setattr(orch, "generate_trip_story_strict", boom)
    with pytest.raises(orch.PipelineAPIError, match="story generation failed"):
        narrate_ready.run(stage="narrate", limit=10)
    assert json.loads((narrate_ready.art / "journal.json").read_text()) == KEEP_JOURNAL
    assert (narrate_ready.art / "story.txt").read_text() == KEEP_STORY


def test_successful_narration_replaces_journal_and_story(narrate_ready, monkeypatch):
    monkeypatch.setattr(
        orch, "generate_all_moments_journal_strict",
        lambda moments: {m["moment"]: f"entry for {m['moment']}" for m in moments},
    )
    monkeypatch.setattr(orch, "generate_trip_story_strict", lambda **kw: "A brand new story.")
    narrate_ready.run(stage="narrate", limit=10)
    journal = json.loads((narrate_ready.art / "journal.json").read_text())
    assert len(journal) == 3 and journal[0]["entry"].startswith("entry for")
    assert (narrate_ready.art / "story.txt").read_text() == "A brand new story."


# --------------------------------------------------------------------------
# Leaf agents: public tool functions keep their lenient behavior
# --------------------------------------------------------------------------

def test_public_narrator_tools_still_fall_back_but_strict_variants_raise(monkeypatch):
    from agents.narrator.tools import journal, story

    class _Boom:
        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("API down")

    monkeypatch.setattr(journal, "get_gemini_client", lambda: _Boom)
    monkeypatch.setattr(story, "get_gemini_client", lambda: _Boom)

    # Agent-facing functions: unchanged contract (fallback, never raise)
    assert journal.generate_all_moments_journal([{"moment": "m", "date": "d", "top_photos": []}]) == {}
    assert "amazing trip" in story.generate_trip_story("[]", "Paris")
    # Pipeline-facing strict variants: errors propagate
    with pytest.raises(RuntimeError):
        journal.generate_all_moments_journal_strict([{"moment": "m", "date": "d", "top_photos": []}])
    with pytest.raises(RuntimeError):
        story.generate_trip_story_strict("[]", "Paris")


# --------------------------------------------------------------------------
# MCP: missed moments only counts moments that are in the journal
# --------------------------------------------------------------------------

def test_mcp_missed_moments_counts_only_journal_moments(tmp_path, monkeypatch):
    import mcp_server
    from agents.memory.tools import memory_helpers

    art = tmp_path / "artefacts"
    art.mkdir()
    # Journal has 3 moments; the scoring manifest also has 2 scenes that never made the journal
    journal = [
        {"moment": "beach", "entry": "e1", "photos": ["a_1.jpg"], "date": "d"},
        {"moment": "hike", "entry": "e2", "photos": ["b_2.jpg"], "date": "d"},
        {"moment": "dinner", "entry": "e3", "photos": ["b_3.jpg"], "date": "d"},
    ]
    manifest = [
        {"filename": "a_1.jpg", "scene_label": "beach", "score": 8.0, "caption": "c1"},
        {"filename": "b_2.jpg", "scene_label": "hike", "score": 7.0, "caption": "c2"},
        {"filename": "b_3.jpg", "scene_label": "dinner", "score": 6.0, "caption": "c3"},
        {"filename": "b_4.jpg", "scene_label": "parking lot", "score": 3.0, "caption": "c4"},
        {"filename": "b_5.jpg", "scene_label": "street", "score": 3.0, "caption": "c5"},
    ]
    (art / "journal.json").write_text(json.dumps(journal))
    (art / "manifest.json").write_text(json.dumps(manifest))

    class _Storage:
        def __init__(self, session_id="x"):
            self.local_base = str(tmp_path)

    class _Memory:
        def __init__(self, session_id):
            pass

        def get_contributor_profile(self, cid):
            return {"name": "A", "moments_present_in": ["beach"]}

    monkeypatch.setattr(mcp_server, "StorageHelper", _Storage)
    monkeypatch.setattr(memory_helpers, "MemoryBankStore", _Memory)

    missed = json.loads(mcp_server.find_missed_moments("a", "x"))
    assert sorted(m["moment"] for m in missed) == ["dinner", "hike"]  # NOT parking lot / street
    assert {m["photo"] for m in missed} == {"b_2.jpg", "b_3.jpg"}


def test_mcp_missed_moments_without_a_journal_reports_not_ready(tmp_path, monkeypatch):
    import mcp_server

    (tmp_path / "artefacts").mkdir()

    class _Storage:
        def __init__(self, session_id="x"):
            self.local_base = str(tmp_path)

    monkeypatch.setattr(mcp_server, "StorageHelper", _Storage)
    assert "No journal generated yet" in json.loads(mcp_server.find_missed_moments("a", "x"))["error"]


# --------------------------------------------------------------------------
# Server-side folder picker: auth, cloud lock-out, photos only
# --------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app import fast_api_app

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("MW_DISABLE_LOCAL_FS", raising=False)
    return TestClient(fast_api_app.app), fast_api_app


def test_local_fs_list_requires_admin_token_when_configured(client, tmp_path, monkeypatch):
    c, _ = client
    (tmp_path / "pic.jpg").write_bytes(b"x")
    monkeypatch.setenv("MW_ADMIN_TOKEN", "s3cret")

    assert c.get("/api/local-fs/list", params={"path": str(tmp_path)}).status_code == 401
    assert c.get("/api/local-fs/list", params={"path": str(tmp_path)},
                 headers={"X-MW-Token": "wrong"}).status_code == 401
    ok = c.get("/api/local-fs/list", params={"path": str(tmp_path)}, headers={"X-MW-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["status"] == "success"


@pytest.mark.parametrize("flag", [{"K_SERVICE": "memoryweaver"}, {"MW_DISABLE_LOCAL_FS": "1"}])
def test_local_fs_unavailable_on_cloud_deployments(client, tmp_path, monkeypatch, flag):
    c, _ = client
    for k, v in flag.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MW_ADMIN_TOKEN", raising=False)

    assert c.get("/api/local-fs/list", params={"path": str(tmp_path)}).status_code == 404
    assert c.post("/api/local-fs/ingest", data={"session_id": "s", "folder_path": str(tmp_path)}).status_code == 404
    assert c.post("/api/local-fs/ingest-files",
                  json={"session_id": "s", "file_paths": [str(tmp_path / "a.jpg")]}).status_code == 404


def test_ingest_files_only_copies_photo_files(client, tmp_path, monkeypatch):
    c, fast_api_app = client
    monkeypatch.delenv("MW_ADMIN_TOKEN", raising=False)

    class _Storage:
        def __init__(self, session_id="x"):
            self.local_base = str(tmp_path / "event")

    monkeypatch.setattr(fast_api_app, "StorageHelper", _Storage)
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.jpg").write_bytes(b"jpg")
    (src / "secrets.txt").write_bytes(b"not a photo")
    (src / "notes.pdf").write_bytes(b"not a photo")

    r = c.post("/api/local-fs/ingest-files", json={
        "session_id": "s",
        "file_paths": [str(src / "ok.jpg"), str(src / "secrets.txt"), str(src / "notes.pdf")],
    })
    assert r.status_code == 200 and r.json()["copied"] == 1
    assert sorted(os.listdir(tmp_path / "event" / "uploads")) == ["ok.jpg"]


# --------------------------------------------------------------------------
# Duplicate detection (embedding model, threshold, cache hygiene)
# --------------------------------------------------------------------------

def _vec_with_cosine(c):
    """A unit vector whose cosine similarity with [1, 0, 0] is exactly c."""
    import math
    return [c, math.sqrt(1 - c * c), 0.0]


def _embed_from(vectors):
    return lambda path: (vectors[os.path.basename(path)], False)


def test_near_duplicates_merge_but_similar_shots_are_kept(event, monkeypatch):
    # aa is the reference; bb is a 0.99-similar burst frame (duplicate);
    # cc is a 0.90-similar *different* shot of the same subject (must be kept).
    monkeypatch.setattr(orch, "get_image_embedding_checked", _embed_from({
        "aa_1.jpg": [1.0, 0.0, 0.0],
        "bb_2.jpg": _vec_with_cosine(0.99),
        "cc_3.jpg": _vec_with_cosine(0.90),
    }))
    stats = event.run(stage="curate", limit=10)
    kept = {h["filename"] for h in json.loads((event.art / "highlights.json").read_text())}
    assert stats["unique"] == 2
    assert kept == {"aa_1.jpg", "cc_3.jpg"}


def test_threshold_sits_between_similar_subjects_and_real_duplicates():
    # Calibration on real photos: same-subject different shots reach ~0.87,
    # re-compressed/resized/cropped copies score >= 0.943.
    assert 0.87 < orch.DUPLICATE_SIMILARITY_THRESHOLD < 0.943


def test_vectors_of_different_sizes_are_never_compared(event, monkeypatch):
    # zip() would truncate and report cosine 1.0 for aa vs bb if they were compared
    monkeypatch.setattr(orch, "get_image_embedding_checked", _embed_from({
        "aa_1.jpg": [1.0, 0.0, 0.0],
        "bb_2.jpg": [1.0, 0.0],
        "cc_3.jpg": [0.0, 1.0, 0.0],
    }))
    assert event.run(stage="curate", limit=10)["unique"] == 3


def test_cached_embeddings_from_another_model_are_recomputed(event, monkeypatch):
    from agents.curator.tools.embed import EMBEDDING_TAG

    (event.art / "curation_cache.json").write_text(json.dumps({
        "aa_1.jpg": {"moderation": {"usable": True, "reason": "ok"}, "embedding": [0.1] * 128},  # untagged (legacy/fake)
        "bb_2.jpg": {"moderation": {"usable": True, "reason": "ok"}, "embedding": [0.1] * 16, "embedding_model": "old-model:16"},
        "cc_3.jpg": {"moderation": {"usable": True, "reason": "ok"}, "embedding": [0.0] * 16, "embedding_model": EMBEDDING_TAG},
    }))
    embedded = []

    def spy(path):
        embedded.append(os.path.basename(path))
        return _good_embed(path)

    monkeypatch.setattr(orch, "get_image_embedding_checked", spy)
    event.run(stage="curate", limit=10)

    assert sorted(embedded) == ["aa_1.jpg", "bb_2.jpg"], "only current-model cache entries may be reused"
    cache = event.cache()
    assert all(cache[p]["embedding_model"] == EMBEDDING_TAG for p in ("aa_1.jpg", "bb_2.jpg"))


def test_embedding_request_uses_gemini_embedding_2_at_768_dims(tmp_path, monkeypatch):
    from agents.curator.tools import embed

    calls = []

    class _Client:
        class models:
            @staticmethod
            def embed_content(**kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(embeddings=[types.SimpleNamespace(values=[0.5] * 768)])

    monkeypatch.setattr(embed, "get_gemini_client", lambda: _Client)
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(photo, "JPEG")

    vec, is_fallback = embed.get_image_embedding_checked(str(photo))
    assert (len(vec), is_fallback) == (768, False)
    assert calls[0]["model"] == "gemini-embedding-2"
    assert calls[0]["config"].output_dimensionality == 768
    assert embed.EMBEDDING_TAG == "gemini-embedding-2:768"


def test_embedding_api_error_is_flagged_as_fallback(tmp_path, monkeypatch):
    from agents.curator.tools import embed

    class _Client:
        class models:
            @staticmethod
            def embed_content(**kwargs):
                raise RuntimeError("404 model not found")

    monkeypatch.setattr(embed, "get_gemini_client", lambda: _Client)
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(photo, "JPEG")
    vec, is_fallback = embed.get_image_embedding_checked(str(photo))
    assert is_fallback is True and len(vec) == 128


# --------------------------------------------------------------------------
# Chronological order: capture times must survive Curate -> manifest -> Proceed
# --------------------------------------------------------------------------

# Capture times chosen so that CHRONOLOGICAL order is the reverse of SCORE order.
_TIMES = {
    "aa_1.jpg": (1_788_000_300.0, "August 30, 2026"),  # latest, highest score
    "bb_2.jpg": (1_788_000_200.0, "August 30, 2026"),
    "cc_3.jpg": (1_788_000_100.0, "August 29, 2026"),  # earliest, lowest score
}
_SCORES = {"aa_1.jpg": 9.0, "bb_2.jpg": 8.0, "cc_3.jpg": 7.0}


def _score_by_filename(batch):
    return [
        {"score": _SCORES[os.path.basename(i["filename"])], "sharpness": 7.0, "composition": 7.0,
         "uniqueness": 7.0, "human_presence": 7.0,
         "scene_label": f"scene {os.path.basename(i['filename'])[:2]}", "caption": "c"}
        for i in batch
    ]


@pytest.fixture
def dated_event(event, monkeypatch):
    monkeypatch.setattr(orch, "get_photo_date_info", lambda path: _TIMES[os.path.basename(path)])
    monkeypatch.setattr(orch, "score_photos_as_judge_batch", _score_by_filename)
    narrated = []

    def journal(moments):
        narrated.extend(m["moment"] for m in moments)
        return {m["moment"]: f"entry for {m['moment']}" for m in moments}

    monkeypatch.setattr(orch, "generate_all_moments_journal_strict", journal)
    monkeypatch.setattr(orch, "generate_trip_story_strict", lambda **kw: "story")
    event.narrated = narrated
    return event


def test_curate_stores_capture_times_in_manifest_and_highlights(dated_event):
    dated_event.run(stage="curate", limit=10)
    for name in ("manifest.json", "highlights.json"):
        entries = json.loads((dated_event.art / name).read_text())
        assert len(entries) == 3
        for e in entries:
            assert (e["timestamp"], e["date"]) == _TIMES[e["filename"]], f"{name}: {e['filename']}"


def test_journal_is_chronological_with_real_dates_not_score_order(dated_event):
    dated_event.run(stage="curate", limit=10)
    dated_event.run(stage="narrate", limit=10)

    # score order would be aa, bb, cc; capture-time order is the reverse
    assert dated_event.narrated == ["scene cc", "scene bb", "scene aa"]
    journal = json.loads((dated_event.art / "journal.json").read_text())
    assert [m["moment"] for m in journal] == ["scene cc", "scene bb", "scene aa"]
    assert [m["date"] for m in journal] == ["August 29, 2026", "August 30, 2026", "August 30, 2026"]
    assert not any(m["date"] == "Unknown Date" for m in journal)


def test_manifest_from_before_the_fix_is_repaired_when_proceeding(dated_event):
    """Events curated before capture times were stored have a manifest with none."""
    old_manifest = [
        {"filename": n, "score": _SCORES[n], "sharpness": 7.0, "composition": 7.0, "uniqueness": 7.0,
         "human_presence": 7.0, "scene_label": f"scene {n[:2]}", "caption": "c", "contributor_id": n[:2]}
        for n in _SCORES
    ]
    assert not any("timestamp" in p for p in old_manifest)
    (dated_event.art / "manifest.json").write_text(json.dumps(old_manifest))
    (dated_event.art / "highlights.json").write_text(json.dumps(old_manifest))

    dated_event.run(stage="narrate", limit=10)  # no re-curate needed

    assert dated_event.narrated == ["scene cc", "scene bb", "scene aa"]
    journal = json.loads((dated_event.art / "journal.json").read_text())
    assert not any(m["date"] == "Unknown Date" for m in journal)


def test_attach_capture_times_fills_only_missing_values(monkeypatch):
    calls = []
    monkeypatch.setattr(orch, "get_photo_date_info", lambda path: calls.append(path) or (5.0, "Jan 1, 2026"))
    photos = [{"filename": "new.jpg"}, {"filename": "done.jpg", "timestamp": 1.0, "date": "Dec 31, 2025"}]

    assert orch._attach_capture_times(photos, "/u") == 1
    assert photos[0]["timestamp"] == 5.0 and photos[0]["date"] == "Jan 1, 2026"
    assert photos[1]["timestamp"] == 1.0, "an existing capture time must not be overwritten"
    assert calls == [os.path.join("/u", "new.jpg")]
