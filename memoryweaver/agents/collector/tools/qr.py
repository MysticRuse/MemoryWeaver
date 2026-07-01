import io
import qrcode
from app.app_utils.storage import StorageHelper

def generate_upload_qr(upload_url: str, event_id: str) -> str:
    """Generates a QR code image pointing to the upload page and saves it to storage."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(upload_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save to a byte buffer
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_bytes = img_byte_arr.getvalue()
    
    # Save to storage (GCS/Local fallback)
    storage = StorageHelper()
    filename = f"qr_{event_id}.png"
    return storage.save_artefact(img_bytes, filename)
