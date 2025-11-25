#!/usr/bin/env python3
"""
One-time authentication script for Google Calendar.
Run this first to authorize and save credentials to token.json
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv
import os

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/calendar']

print("=" * 60)
print("Google Calendar Authentication")
print("=" * 60)
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
print("You can now use the calendar functions in client.py")
print("=" * 60)
