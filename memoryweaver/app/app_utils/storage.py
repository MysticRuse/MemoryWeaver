import os
import shutil
from google.cloud import storage

class StorageHelper:
    def __init__(self):
        self.bucket_name = os.environ.get("GCS_BUCKET_NAME")
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        
        # Anchor local_base to the memoryweaver project root directory
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.local_base = os.path.join(project_root, "local_storage")
        if self.bucket_name:
            try:
                self.storage_client = storage.Client(project=self.project_id)
                self.bucket = self.storage_client.bucket(self.bucket_name)
                self.use_gcs = True
            except Exception as e:
                print(f"Failed to initialize GCS client: {e}. Falling back to local storage.")
                self.use_gcs = False
        else:
            self.use_gcs = False

        if not self.use_gcs:
            os.makedirs(os.path.join(self.local_base, "uploads"), exist_ok=True)
            os.makedirs(os.path.join(self.local_base, "thumbs"), exist_ok=True)
            os.makedirs(os.path.join(self.local_base, "artefacts"), exist_ok=True)

    def save_upload(self, file_bytes: bytes, filename: str) -> str:
        """Saves an uploaded photo and returns its GCS URI or local absolute filepath."""
        if self.use_gcs:
            blob = self.bucket.blob(f"uploads/{filename}")
            blob.upload_from_string(file_bytes)
            return f"gs://{self.bucket_name}/uploads/{filename}"
        else:
            dest_path = os.path.join(self.local_base, "uploads", filename)
            with open(dest_path, "wb") as f:
                f.write(file_bytes)
            return dest_path

    def save_thumbnail(self, file_bytes: bytes, filename: str) -> str:
        """Saves a thumbnail image and returns its location."""
        if self.use_gcs:
            blob = self.bucket.blob(f"thumbs/{filename}")
            blob.upload_from_string(file_bytes)
            return f"gs://{self.bucket_name}/thumbs/{filename}"
        else:
            dest_path = os.path.join(self.local_base, "thumbs", filename)
            with open(dest_path, "wb") as f:
                f.write(file_bytes)
            return dest_path

    def save_artefact(self, content_bytes: bytes, filename: str) -> str:
        """Saves a generated trip artifact (journal, reel JSON, etc.) and returns its location."""
        if self.use_gcs:
            blob = self.bucket.blob(f"artefacts/{filename}")
            blob.upload_from_string(content_bytes)
            return f"gs://{self.bucket_name}/artefacts/{filename}"
        else:
            dest_path = os.path.join(self.local_base, "artefacts", filename)
            with open(dest_path, "wb") as f:
                f.write(content_bytes)
            return dest_path
