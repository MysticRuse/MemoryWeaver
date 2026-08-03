"""Batch reclassify: submit skips anything the free local heuristics already
resolved, and poll only writes classifications for jobs that actually
succeeded - a job still running or one Gemini failed must not touch the vault.
"""

import io
import json

import pytest
from PIL import Image

from app.app_utils.crypto import encrypt_bytes


class FakeJobState:
    def __init__(self, name):
        self.name = name


class FakeInlinedResponse:
    def __init__(self, filename, text=None, error=None):
        self.metadata = {"filename": filename}
        self.response = type("R", (), {"text": text})() if text is not None else None
        self.error = error


class FakeDest:
    def __init__(self, inlined_responses):
        self.inlined_responses = inlined_responses


class FakeBatchJob:
    def __init__(self, name, state="JOB_STATE_PENDING", inlined_responses=None):
        self.name = name
        self.state = FakeJobState(state)
        self.dest = FakeDest(inlined_responses or [])

    @property
    def done(self):
        return self.state.name in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED")


class FakeBatches:
    def __init__(self):
        self.created_with = None
        self.job = None

    def create(self, model=None, src=None, config=None):
        self.created_with = {"model": model, "src": src, "config": config}
        self.job = FakeBatchJob("batches/fake-job-1")
        return self.job

    def get(self, name=None):
        return self.job


class FakeBatchClient:
    def __init__(self):
        self.batches = FakeBatches()


@pytest.fixture
def batch_library(tmp_path, monkeypatch):
    import app.app_utils.storage as storage_mod
    import app.services.batch_reclassify as batch_mod
    import app.services.classification as classification_mod
    import app.services.media_store as media_mod

    base = tmp_path / "local_storage"
    (base / "uploads").mkdir(parents=True, exist_ok=True)

    class TmpStorage(storage_mod.StorageHelper):
        def __init__(self, session_id="default"):
            self.session_id = session_id
            self.bucket_name = None
            self.project_id = None
            self.local_base = str(base)
            self._gcs_prefix = ""
            self.use_gcs = False

    for mod in (storage_mod, batch_mod, classification_mod, media_mod):
        if hasattr(mod, "StorageHelper"):
            monkeypatch.setattr(mod, "StorageHelper", TmpStorage)

    vault_file = base / "cleaner_vault.json"
    monkeypatch.setattr(batch_mod, "get_global_vault_file_path", lambda: str(vault_file))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "teal").save(buf, format="JPEG")
    (base / "uploads" / "img_one.jpg").write_bytes(encrypt_bytes(buf.getvalue(), "default"))
    (base / "uploads" / "clip.mp4").write_bytes(b"not a real video")

    return vault_file


def test_video_resolves_locally_without_a_batch_request(batch_library, monkeypatch):
    import app.services.batch_reclassify as batch_mod

    fake_client = FakeBatchClient()
    monkeypatch.setattr(batch_mod, "get_gemini_client", lambda: fake_client)

    result = batch_mod.submit_batch_reclassify("default", ["clip.mp4"])

    assert result["job_name"] is None
    assert result["resolved_locally"]["clip.mp4"]["category"] == "other"
    assert fake_client.batches.created_with is None


def test_unresolved_photo_is_submitted_as_a_batch_job(batch_library, monkeypatch):
    import app.services.batch_reclassify as batch_mod

    fake_client = FakeBatchClient()
    monkeypatch.setattr(batch_mod, "get_gemini_client", lambda: fake_client)

    result = batch_mod.submit_batch_reclassify("default", ["img_one.jpg"])

    assert result["job_name"] == "batches/fake-job-1"
    assert result["submitted"] == ["img_one.jpg"]
    assert fake_client.batches.created_with["src"][0].metadata == {"filename": "img_one.jpg"}


def test_pending_job_leaves_the_vault_untouched(batch_library, monkeypatch):
    import app.services.batch_reclassify as batch_mod

    fake_client = FakeBatchClient()
    monkeypatch.setattr(batch_mod, "get_gemini_client", lambda: fake_client)
    batch_mod.submit_batch_reclassify("default", ["img_one.jpg"])

    status = batch_mod.poll_batch_reclassify("default", "batches/fake-job-1")

    assert status["status"] == "pending"
    assert not batch_library.exists()


def test_succeeded_job_writes_classifications_into_the_vault(batch_library, monkeypatch):
    import app.services.batch_reclassify as batch_mod

    fake_client = FakeBatchClient()
    monkeypatch.setattr(batch_mod, "get_gemini_client", lambda: fake_client)
    batch_mod.submit_batch_reclassify("default", ["img_one.jpg"])

    fake_client.batches.job.state = FakeJobState("JOB_STATE_SUCCEEDED")
    fake_client.batches.job.dest = FakeDest([
        FakeInlinedResponse(
            "img_one.jpg",
            text=json.dumps(
                {
                    "primary_category": "pets",
                    "secondary_tags": [],
                    "confidence": 0.9,
                    "needs_review": False,
                    "reason": "Dog on a walk",
                    "caption": "Good boy!",
                    "extracted_text": "",
                }
            ),
        )
    ])

    status = batch_mod.poll_batch_reclassify("default", "batches/fake-job-1")

    assert status["status"] == "done"
    assert status["applied"] == ["img_one.jpg"]
    vault = json.loads(batch_library.read_text())
    assert vault["classifications"]["img_one.jpg"]["category"] == "pets"


def test_failed_item_in_a_succeeded_job_is_skipped_not_written(batch_library, monkeypatch):
    import app.services.batch_reclassify as batch_mod

    fake_client = FakeBatchClient()
    monkeypatch.setattr(batch_mod, "get_gemini_client", lambda: fake_client)
    batch_mod.submit_batch_reclassify("default", ["img_one.jpg"])

    fake_client.batches.job.state = FakeJobState("JOB_STATE_SUCCEEDED")
    fake_client.batches.job.dest = FakeDest([
        FakeInlinedResponse("img_one.jpg", error="quota exceeded")
    ])

    status = batch_mod.poll_batch_reclassify("default", "batches/fake-job-1")

    assert status["status"] == "done"
    assert status["applied"] == []
    vault = json.loads(batch_library.read_text())
    assert "img_one.jpg" not in vault["classifications"]
