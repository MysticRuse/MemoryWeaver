"""Request bodies for the HTTP API.

Extracted from fast_api_app.py so routers can import them without a
circular dependency on the application module.
"""

from typing import Any

from pydantic import BaseModel


class IngestRequest(BaseModel):
    filenames: list[str]
    session_id: str = "default"


class PhotoActionRequest(BaseModel):
    session_id: str
    filename: str
    action: str
    text_context: str = None


class CreateSessionRequest(BaseModel):
    name: str
    event_type: str = "trip"


class RenameSessionRequest(BaseModel):
    name: str


class UpdateSessionRequest(BaseModel):
    session_id: str
    name: str
    event_type: str = "trip"


class MagicEnhanceRequest(BaseModel):
    filename: str
    session_id: str = "default"


class SelectPhotoVersionRequest(BaseModel):
    filename: str
    version: str  # "original" or "enhanced"
    session_id: str = "default"


class IngestFilesBody(BaseModel):
    session_id: str
    file_paths: list[str]


class SwapPhotoBody(BaseModel):
    session_id: str
    old_photo: str
    new_photo: str


class MovePhotoBody(BaseModel):
    session_id: str
    filename: str
    action: str  # "promote" or "demote"


class UpdateCaptionBody(BaseModel):
    session_id: str
    filename: str
    caption: str


class DescribePhotoBody(BaseModel):
    session_id: str
    filename: str


class CleanerSaveNoteRequest(BaseModel):
    session_id: str
    filename: str
    title: str
    note_content: str
    password: str = None
    purge_photo: bool = False


class CleanerLockPhotoRequest(BaseModel):
    session_id: str
    filename: str
    password: str
    lock: bool


class UnlockItemRequest(BaseModel):
    session_id: str
    filename: str
    item_type: str
    password: str


class CleanerAnalyzeRequest(BaseModel):
    session_id: str
    force_refresh: bool = False


class CompressVideosRequest(BaseModel):
    session_id: str
    filenames: list[str]


class ResolveKeepDeleteRequest(BaseModel):
    original_filename: str
    processed_filename: str
    choice: str # "keep_both", "delete_original", "delete_processed"
    session_id: str


class VideoDescribeRequest(BaseModel):
    filename: str
    session_id: str


class UpdateClassificationRequest(BaseModel):
    session_id: str
    filename: str
    category: str
    subcategory: str | None = None


class ConvertLivePhotosRequest(BaseModel):
    session_id: str
    video_filenames: list[str]


class PurgeStagedRequest(BaseModel):
    session_id: str
    filenames: list[str]


class AddFrameToAlbumRequest(BaseModel):
    session_id: str
    original_video_filename: str
    base64_data: str


class SyncConfigRequest(BaseModel):
    session_id: str
    target_folder: str


class SplitVideoRequest(BaseModel):
    filename: str
    session_id: str
    num_frames: int = 10


class RemoveWatermarkRequest(BaseModel):
    filename: str
    session_id: str
    remove_watermarks: bool = True
    remove_logos: bool = True
    remove_captions: bool = True
    remove_overlays: bool = True
    remove_voice: bool = True
    trim_outro: bool = True


class TransformPhotoRequest(BaseModel):
    filename: str
    session_id: str
    type: str = "all"


class SaveTransformRequest(BaseModel):
    session_id: str
    temp_filename: str
    original_filename: str
    transformation_type: str
    custom_caption: str | None = None
    caption_x: float | None = None
    caption_y: float | None = None
    caption_color: list[int] | None = None
    stickers: list[dict[str, Any]] | None = None
    is_edited: bool | None = False


class NanoSuggestionsRequest(BaseModel):
    filename: str
    session_id: str = "default"
    type: str = "kids"


class RenamePhotoRequest(BaseModel):
    session_id: str
    old_filename: str
    new_filename: str
