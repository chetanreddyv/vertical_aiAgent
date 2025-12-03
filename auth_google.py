#!/usr/bin/env python3
"""
Unified authentication script for all Google services (Calendar, Drive, Docs).
Run this once to authorize and save credentials to token.json for all Google services.
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv
import os

load_dotenv()

# Combined scopes for all Google services
SCOPES = [
    'https://www.googleapis.com/auth/calendar',           # Google Calendar
    'https://www.googleapis.com/auth/drive',              # Google Drive (full access)
    'https://www.googleapis.com/auth/documents',          # Google Docs
]

print("=" * 60)
print("Google Workspace Authentication")
print("=" * 60)
print()
print("This will authenticate for:")
print("  ✓ Google Calendar")
print("  ✓ Google Drive")
print("  ✓ Google Docs")
print()

client_config = {
    "installed": {
        "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    }
}

print("Starting OAuth flow...")
print("A browser window will open for authorization.")
print()

flow = InstalledAppFlow.from_client_config(
    client_config, SCOPES)

print("Opening browser for authorization...")
print("Make sure you've added http://localhost:8090 to your OAuth redirect URIs")
print()
creds = flow.run_local_server(port=8090)

# Save the credentials
with open('token.json', 'w') as token:
    token.write(creds.to_json())

print()
print("✅ Authorization successful!")
print("✅ Credentials saved to token.json")
print()
print("You can now use all Google Workspace services:")
print("  • Google Calendar (calendar_server.py)")
print("  • Google Drive (drive_server.py)")
print("  • Google Docs (docs_server.py)")
print("=" * 60)
