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
    'https://www.googleapis.com/auth/gmail.modify',       # Gmail modify
    'https://www.googleapis.com/auth/gmail.send',         # Gmail send
]

print("=" * 60)
print("Google Workspace Authentication")
print("=" * 60)
print()
print("This will authenticate for:")
print("  ✓ Google Calendar")
print("  ✓ Google Drive")
print("  ✓ Google Docs")
print("  ✓ Gmail")
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

# ── Print base64-encoded token for Azure deployment ───────────────────────────
import base64
with open('token.json', 'rb') as f:
    token_b64 = base64.b64encode(f.read()).decode()

print("=" * 60)
print("📋 AZURE DEPLOYMENT — Copy this value into Azure secrets")
print("   Secret name: GOOGLE_TOKEN_JSON")
print("=" * 60)
print(token_b64)
print("=" * 60)
print()
print("You can now use all Google Workspace services:")
print("  • Google Calendar (calendar_server.py)")
print("  • Google Drive (drive_server.py)")
print("  • Gmail (email_server.py)")
print("=" * 60)
