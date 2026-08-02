import os

from google.cloud import storage

from app.app_utils.logging_config import get_logger

logger = get_logger(__name__)


class StorageHelper:
    """
    Reads/writes trip media for a single session. The 'default' session_id maps
    onto the original flat local_storage/{uploads,thumbs,artefacts} layout for
    backward compatibility with pre-existing installs; any other session_id gets
    its own isolated local_storage/sessions/<session_id>/ namespace (and matching
    GCS prefix), so multiple events can be curated without clobbering each other.
    """

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.bucket_name = os.environ.get("GCS_BUCKET_NAME")
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")

        # Anchor local_base to the memoryweaver project root directory
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        storage_root = os.path.join(project_root, "local_storage")

        if session_id == "default":
            self.local_base = storage_root
            self._gcs_prefix = ""
        else:
            self.local_base = os.path.join(storage_root, "sessions", session_id)
            self._gcs_prefix = f"sessions/{session_id}/"

        if self.bucket_name:
            try:
                self.storage_client = storage.Client(project=self.project_id)
                self.bucket = self.storage_client.bucket(self.bucket_name)
                self.use_gcs = True
            except Exception as e:
                logger.warning(f"Failed to initialize GCS client: {e}. Falling back to local storage.")
                self.use_gcs = False
        else:
            self.use_gcs = False

        if not self.use_gcs:
            os.makedirs(os.path.join(self.local_base, "uploads"), exist_ok=True)
            os.makedirs(os.path.join(self.local_base, "thumbs"), exist_ok=True)
            os.makedirs(os.path.join(self.local_base, "artefacts"), exist_ok=True)

    def gcs_path(self, relative_name: str) -> str:
        """Builds the GCS blob path for a file scoped to this session."""
        return f"{self._gcs_prefix}{relative_name}"

    def save_upload(self, file_bytes: bytes, filename: str) -> str:
        """Saves an uploaded photo and returns its GCS URI or local absolute filepath."""
        if self.use_gcs:
            blob_name = self.gcs_path(f"uploads/{filename}")
            self.bucket.blob(blob_name).upload_from_string(file_bytes)
            return f"gs://{self.bucket_name}/{blob_name}"
        else:
            dest_path = os.path.join(self.local_base, "uploads", filename)
            with open(dest_path, "wb") as f:
                f.write(file_bytes)
            return dest_path

    def save_thumbnail(self, file_bytes: bytes, filename: str) -> str:
        """Saves a thumbnail image and returns its location."""
        if self.use_gcs:
            blob_name = self.gcs_path(f"thumbs/{filename}")
            self.bucket.blob(blob_name).upload_from_string(file_bytes)
            return f"gs://{self.bucket_name}/{blob_name}"
        else:
            dest_path = os.path.join(self.local_base, "thumbs", filename)
            with open(dest_path, "wb") as f:
                f.write(file_bytes)
            return dest_path

    def save_artefact(self, content_bytes: bytes, filename: str) -> str:
        """Saves a generated trip artifact (journal, reel JSON, etc.) and returns its location."""
        if self.use_gcs:
            blob_name = self.gcs_path(f"artefacts/{filename}")
            self.bucket.blob(blob_name).upload_from_string(content_bytes)
            return f"gs://{self.bucket_name}/{blob_name}"
        else:
            dest_path = os.path.join(self.local_base, "artefacts", filename)
            with open(dest_path, "wb") as f:
                f.write(content_bytes)
            return dest_path
