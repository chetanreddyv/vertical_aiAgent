from fastmcp import FastMCP
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from dotenv import load_dotenv
import os
import io
import re
import asyncio
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional, List, Tuple, Dict, Any

# Load environment variables
load_dotenv()

# Scopes for Google Drive
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/documents'
]



mcp = FastMCP("Google Drive Server")

def build_drive_list_params(
    query: str,
    page_size: int = 10,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Constructs the dictionary of parameters for service.files().list().
    """
    params = {
        "q": query,
        "pageSize": page_size,
        "fields": "files(id, name, mimeType, size, modifiedTime, webViewLink)",
        "includeItemsFromAllDrives": include_items_from_all_drives,
        "supportsAllDrives": True,
    }

    # If a specific drive_id is provided, we must set corpora='drive'
    # and include the driveId.
    if drive_id:
        params["driveId"] = drive_id
        params["corpora"] = "drive"
    else:
        # If no drive_id, use the provided corpora or default to 'user' (implicitly)
        # 'allDrives' is expensive, so use with care.
        if corpora:
            params["corpora"] = corpora

    return params

async def resolve_drive_item(
    service,
    item_id_or_name: str,
    extra_fields: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """
    Resolves a file/folder ID or name to a canonical ID and metadata.
    If 'item_id_or_name' looks like an ID, verifies it.
    If it looks like a name, searches for it.
    """
    # Simple heuristic: Drive IDs are usually alphanumeric, ~33 chars, no spaces.
    # But names can be anything. We'll try to 'get' it as an ID first.
    
    fields = "id, name, mimeType"
    if extra_fields:
        fields += f", {extra_fields}"

    try:
        # 1. Try treating it as an ID
        file_meta = await asyncio.to_thread(
            service.files().get(fileId=item_id_or_name, fields=fields, supportsAllDrives=True).execute
        )
        return item_id_or_name, file_meta
    except Exception:
        # 2. If that fails, treat it as a name search
        # We'll look for exact name match first, not trashed
        escaped_name = item_id_or_name.replace("'", "\\'")
        query = f"name = '{escaped_name}' and trashed = false"
        results = await asyncio.to_thread(
            service.files().list(
                q=query,
                pageSize=1,
                fields=f"files({fields})",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True
            ).execute
        )
        files = results.get('files', [])
        if not files:
            raise ValueError(f"Could not find file or folder with ID or name '{item_id_or_name}'")
        
        return files[0]['id'], files[0]

def extract_office_xml_text(content_bytes: bytes, mime_type: str) -> Optional[str]:
    """
    Extracts plain text from Office Open XML files (docx, xlsx, pptx)
    by unzipping and parsing the main XML component.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
            if "wordprocessingml.document" in mime_type:
                # Word: word/document.xml
                xml_content = z.read("word/document.xml")
                root = ET.fromstring(xml_content)
                # Text is in <w:t> tags
                # Namespace usually: {http://schemas.openxmlformats.org/wordprocessingml/2006/main}
                # We'll just search for all 't' tags regardless of namespace for simplicity,
                # or strictly use the namespace if needed.
                # A simple reliable way is to iterate all elements and check tag name ending in 't'
                text_parts = []
                for elem in root.iter():
                    if elem.tag.endswith("}t") and elem.text:
                        text_parts.append(elem.text)
                return "\n".join(text_parts)

            elif "spreadsheetml.sheet" in mime_type:
                # Excel: xl/sharedStrings.xml contains the text strings usually
                # But actual data is in worksheets. This is complex.
                # Simplified approach: read sharedStrings if available
                text_parts = []
                if "xl/sharedStrings.xml" in z.namelist():
                    xml_content = z.read("xl/sharedStrings.xml")
                    root = ET.fromstring(xml_content)
                    for elem in root.iter():
                        if elem.tag.endswith("}t") and elem.text:
                            text_parts.append(elem.text)
                return "\n".join(text_parts)

            elif "presentationml.presentation" in mime_type:
                # PowerPoint: ppt/slides/slide*.xml
                text_parts = []
                for name in z.namelist():
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                        xml_content = z.read(name)
                        root = ET.fromstring(xml_content)
                        for elem in root.iter():
                            # Text in <a:t>
                            if elem.tag.endswith("}t") and elem.text:
                                text_parts.append(elem.text)
                return "\n".join(text_parts)

    except Exception as e:
        print(f"Error extracting Office XML: {e}")
        return None
    return None

def get_drive_service():
    """Get authenticated Google Drive service.
    
    Returns the drive service or raises an error if not authenticated.
    """
    creds = None
    # The file token.json stores the user's access and refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    else:
        raise Exception(
            "No authentication found. Please run 'python auth_google.py' first to authenticate."
        )
    
    # If credentials expired, try to refresh
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save the refreshed credentials
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        else:
            raise Exception(
                "Authentication expired. Please run 'python auth_google.py' again."
            )

    service = build('drive', 'v3', credentials=creds)
    return service

@mcp.tool()
def list_files(max_results: int = 10, folder_id: str = None) -> str:
    """List files in Google Drive.
    
    Args:
        max_results: Maximum number of files to return (default: 10)
        folder_id: Optional folder ID to list files from (default: all files)
    """
    try:
        service = get_drive_service()
        
        # Build query
        query = "trashed=false"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        
        results = service.files().list(
            pageSize=max_results,
            q=query,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)"
        ).execute()
        
        files = results.get('files', [])
        
        if not files:
            return "No files found."
        
        result = "Files in Google Drive:\n"
        for file in files:
            file_type = "📁 Folder" if file['mimeType'] == 'application/vnd.google-apps.folder' else "📄 File"
            size = file.get('size', 'N/A')
            if size != 'N/A':
                size = f"{int(size) / 1024:.2f} KB"
            result += f"- {file_type}: {file['name']} (ID: {file['id']}, Size: {size})\n"
            result += f"  Modified: {file.get('modifiedTime', 'N/A')}\n"
            result += f"  Link: {file.get('webViewLink', 'N/A')}\n"
        
        return result
    except Exception as e:
        return f"Error listing files: {str(e)}"

@mcp.tool()
async def search_files(
    query: str,
    max_results: int = 10,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> str:
    """
    Searches for files and folders within a user's Google Drive, including shared drives.

    Args:
        query (str): The search query. Can be a structured Drive query (e.g., "name contains 'report'") 
                     or a simple natural language search (e.g., "project plan").
        max_results (int): The maximum number of files to return. Defaults to 10.
        drive_id (Optional[str]): ID of the shared drive to search. If None, behavior depends on `corpora` and `include_items_from_all_drives`.
        include_items_from_all_drives (bool): Whether shared drive items should be included in results. Defaults to True.
        corpora (Optional[str]): Bodies of items to query (e.g., 'user', 'domain', 'drive', 'allDrives').
    """
    try:
        service = get_drive_service()
        
        # Helper to execute search
        async def execute_search(q_str):
            list_params = build_drive_list_params(
                query=q_str,
                page_size=max_results,
                drive_id=drive_id,
                include_items_from_all_drives=include_items_from_all_drives,
                corpora=corpora,
            )
            return await asyncio.to_thread(service.files().list(**list_params).execute)

        try:
            # 1. Try query as-is (plus trashed filter)
            # This handles valid Drive syntax queries like "name = 'foo'"
            final_query = f"({query}) and trashed=false"
            results = await execute_search(final_query)
        except Exception:
            # 2. Fallback: Treat as free text search
            # Escape single quotes to prevent syntax errors
            escaped_query = query.replace("'", "\\'")
            # Broader search: name or content
            final_query = f"(name contains '{escaped_query}' or fullText contains '{escaped_query}') and trashed=false"
            results = await execute_search(final_query)
        
        files = results.get('files', [])
        
        if not files:
            return f"No files found matching '{query}'."
        
        result = f"Search results for '{query}':\n"
        for file in files:
            file_type = "📁 Folder" if file['mimeType'] == 'application/vnd.google-apps.folder' else "📄 File"
            size = file.get('size', 'N/A')
            if size != 'N/A':
                size = f"{int(size) / 1024:.2f} KB"
            result += f"- {file_type}: {file['name']} (ID: {file['id']}, Size: {size})\n"
            result += f"  Link: {file.get('webViewLink', 'N/A')}\n"
        
        return result
    except Exception as e:
        return f"Error searching files: {str(e)}"

@mcp.tool()
async def get_file_content(file_id: str) -> str:
    """
    Retrieves the content of a specific Google Drive file by ID, supporting files in shared drives.
    
    Args:
        file_id: Drive file ID or name.
    """
    try:
        service = get_drive_service()
        
        resolved_file_id, file_metadata = await resolve_drive_item(
            service,
            file_id,
            extra_fields="name, webViewLink",
        )
        file_id = resolved_file_id
        mime_type = file_metadata.get("mimeType", "")
        file_name = file_metadata.get("name", "Unknown File")
        
        export_mime_type = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }.get(mime_type)

        if export_mime_type:
            request_obj = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        else:
            request_obj = service.files().get_media(fileId=file_id)
            
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request_obj)
        
        # Download in chunks
        done = False
        while not done:
            status, done = await asyncio.to_thread(downloader.next_chunk)

        file_content_bytes = fh.getvalue()

        # Attempt Office XML extraction only for actual Office XML files
        office_mime_types = {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        }

        body_text = ""
        if mime_type in office_mime_types:
            office_text = extract_office_xml_text(file_content_bytes, mime_type)
            if office_text:
                body_text = office_text
            else:
                # Fallback: try UTF-8; otherwise flag binary
                try:
                    body_text = file_content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    body_text = (
                        f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                        f"{len(file_content_bytes)} bytes]"
                    )
        else:
            # For non-Office files (including Google native files), try UTF-8 decode directly
            try:
                body_text = file_content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_text = (
                    f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                    f"{len(file_content_bytes)} bytes]"
                )

        # Assemble response
        header = (
            f'File: "{file_name}" (ID: {file_id}, Type: {mime_type})\n'
            f'Link: {file_metadata.get("webViewLink", "#")}\n\n--- CONTENT ---\n'
        )
        return header + body_text
    except Exception as e:
        return f"Error getting file content: {str(e)}"

@mcp.tool()
def create_folder(name: str, parent_folder_id: str = None) -> str:
    """Create a new folder in Google Drive.
    
    Args:
        name: Name of the folder to create
        parent_folder_id: Optional parent folder ID (default: root)
    """
    try:
        service = get_drive_service()
        
        file_metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        
        if parent_folder_id:
            file_metadata['parents'] = [parent_folder_id]
        
        folder = service.files().create(
            body=file_metadata,
            fields='id, name, webViewLink'
        ).execute()
        
        return f"✅ Folder created: {folder['name']}\nFolder ID: {folder['id']}\nLink: {folder.get('webViewLink', 'N/A')}"
    except Exception as e:
        return f"Error creating folder: {str(e)}"

@mcp.tool()
def get_file_metadata(file_id: str) -> str:
    """Get metadata for a specific file.
    
    Args:
        file_id: ID of the file
    """
    try:
        service = get_drive_service()
        
        file = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, createdTime, owners, webViewLink, webContentLink"
        ).execute()
        
        result = f"File Metadata:\n"
        result += f"Name: {file['name']}\n"
        result += f"ID: {file['id']}\n"
        result += f"Type: {file['mimeType']}\n"
        result += f"Size: {file.get('size', 'N/A')} bytes\n"
        result += f"Created: {file.get('createdTime', 'N/A')}\n"
        result += f"Modified: {file.get('modifiedTime', 'N/A')}\n"
        result += f"Owner: {file.get('owners', [{}])[0].get('displayName', 'N/A')}\n"
        result += f"View Link: {file.get('webViewLink', 'N/A')}\n"
        result += f"Download Link: {file.get('webContentLink', 'N/A')}\n"
        
        return result
    except Exception as e:
        return f"Error getting file metadata: {str(e)}"

@mcp.tool()
def upload_file(file_path: str, folder_id: str = None, file_name: str = None) -> str:
    """Upload a file to Google Drive.
    
    Args:
        file_path: Local path to the file to upload
        folder_id: Optional folder ID to upload to (default: root)
        file_name: Optional name for the file in Drive (default: original filename)
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"
        
        service = get_drive_service()
        
        file_metadata = {
            'name': file_name or os.path.basename(file_path)
        }
        
        if folder_id:
            file_metadata['parents'] = [folder_id]
        
        media = MediaFileUpload(file_path, resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name, webViewLink, size'
        ).execute()
        
        size = f"{int(file.get('size', 0)) / 1024:.2f} KB"
        return f"✅ File uploaded: {file['name']}\nFile ID: {file['id']}\nSize: {size}\nLink: {file.get('webViewLink', 'N/A')}"
    except Exception as e:
        return f"Error uploading file: {str(e)}"

@mcp.tool()
def download_file(file_id: str, destination_path: str) -> str:
    """Download a file from Google Drive.
    
    Args:
        file_id: ID of the file to download
        destination_path: Local path where the file should be saved
    """
    try:
        service = get_drive_service()
        
        # Get file metadata
        file = service.files().get(fileId=file_id, fields='name, mimeType').execute()
        
        # Check if it's a Google Workspace file (Docs, Sheets, etc.)
        if file['mimeType'].startswith('application/vnd.google-apps'):
            return f"Error: Cannot download Google Workspace files directly. Please export them instead.\nFile type: {file['mimeType']}"
        
        request = service.files().get_media(fileId=file_id)
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        # Save to file
        with open(destination_path, 'wb') as f:
            f.write(fh.getvalue())
        
        file_size = os.path.getsize(destination_path)
        size_str = f"{file_size / 1024:.2f} KB"
        
        return f"✅ File downloaded: {file['name']}\nSaved to: {destination_path}\nSize: {size_str}"
    except Exception as e:
        return f"Error downloading file: {str(e)}"

if __name__ == "__main__":
    mcp.run()
