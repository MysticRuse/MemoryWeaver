import os
import io
import hashlib
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from agents.memory.tools.memory_bank import MemoryBankStore
from agents.collector.tools.upload import process_and_save_upload

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def get_drive_service():
    """Authenticates the user and returns the Google Drive API service client."""
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first time.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                raise FileNotFoundError(
                    "Error: 'credentials.json' file not found in project root. "
                    "Please download OAuth client secrets from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)

def download_and_ingest_drive_folder(folder_id: str, project_root: str):
    """
    Downloads all images from a Google Drive folder, extracts uploader names
    from file metadata, and saves them to local_storage/uploads/.
    """
    service = get_drive_service()
    
    # Query to list all files in the specified folder
    query = f"'{folder_id}' in parents and mimeType startswith 'image/' and trashed = false"
    
    # We request the 'owners' field to get uploader names
    results = service.files().list(
        q=query,
        fields="nextPageToken, files(id, name, owners, mimeType)",
        pageSize=100
    ).execute()
    
    files = results.get('files', [])
    if not files:
        print("No image files found in the specified Google Drive folder.")
        return

    print(f"Found {len(files)} photos in Google Drive folder. Downloading & indexing...")
    
    store = MemoryBankStore()
    uploads_count = 0
    
    for f in files:
        file_id = f['id']
        original_name = f['name']
        
        # Extract owner/uploader display name
        owners = f.get('owners', [])
        uploader_name = owners[0].get('displayName', 'Anonymous') if owners else 'Anonymous'
        
        # Download the file content in-memory
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            
        file_bytes = fh.getvalue()
        
        # Process and save the file using the existing Collector Agent upload logic
        try:
            info = process_and_save_upload(
                file_bytes=file_bytes,
                original_filename=original_name,
                contributor_name=uploader_name
            )
            
            # Map contributor name to ID in Memory Bank
            store.upsert_contributor(info["contributor_id"], uploader_name, [], 0)
            
            print(f"  [SAVED] {original_name} -> Uploaded by {uploader_name} (ID: {info['contributor_id']})")
            uploads_count += 1
        except Exception as ex:
            print(f"  [ERROR] Failed to ingest {original_name}: {ex}")

    print(f"\nSuccessfully downloaded and ingested {uploads_count} photos from Google Drive!")

if __name__ == '__main__':
    # Example usage:
    # Set the folder ID here or pass as parameter
    import sys
    if len(sys.argv) < 2:
        print("Usage: python drive_downloader.py <GOOGLE_DRIVE_FOLDER_ID>")
    else:
        folder_id = sys.argv[1]
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        download_and_ingest_drive_folder(folder_id, project_root)
