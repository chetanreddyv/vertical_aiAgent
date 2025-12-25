from fastmcp import FastMCP
import os.path
import datetime
import uuid
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
import os
from typing import List

# Load environment variables
load_dotenv()

# If modifying these scopes, delete the file token.json.
# Using unified scopes for all Google services (managed by auth_google.py)
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents'
]

mcp = FastMCP("Google Calendar Server")

def get_calendar_service():
    """Get authenticated Google Calendar service.
    
    Returns the calendar service or raises an error if not authenticated.
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

    service = build('calendar', 'v3', credentials=creds)
    return service

@mcp.tool()
def list_events(max_results: int = 10) -> str:
    """List upcoming calendar events.
    
    Args:
        max_results: Maximum number of events to return (default: 10)
    """
    try:
        service = get_calendar_service()
        now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
        
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=max_results, singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            return "No upcoming events found."

        result = "Upcoming events:\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            result += f"- {start}: {event['summary']}\n"
            
        return result
    except Exception as e:
        return f"Error listing events: {str(e)}"

@mcp.tool()
def create_event(summary: str, start_time: str, end_time: str, description: str = "") -> str:
    """Create a new calendar event.
    
    Args:
        summary: Title of the event
        start_time: Start time in ISO format (e.g., '2023-10-27T10:00:00')
        end_time: End time in ISO format
        description: Optional description of the event
    """
    try:
        service = get_calendar_service()
        
        event = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_time,
                'timeZone': 'UTC', # You might want to make this configurable
            },
            'end': {
                'dateTime': end_time,
                'timeZone': 'UTC',
            },
        }

        event = service.events().insert(calendarId='primary', body=event).execute()
        return f"Event created: {event.get('htmlLink')}"
    except Exception as e:
        return f"Error creating event: {str(e)}"

@mcp.tool()
def create_meeting(summary: str, start_time: str, end_time: str, attendees: List[str] = [], description: str = "") -> str:
    """Create a new Google Meet video conference.
    
    Args:
        summary: Title of the meeting
        start_time: Start time in ISO format (e.g., '2023-10-27T10:00:00')
        end_time: End time in ISO format
        attendees: List of email addresses to invite
        description: Optional description of the meeting
    """
    try:
        service = get_calendar_service()
        
        event = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_time,
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_time,
                'timeZone': 'UTC',
            },
            'conferenceData': {
                'createRequest': {
                    'requestId': str(uuid.uuid4()),
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                }
            },
            'attendees': [{'email': email} for email in attendees],
        }

        # conferenceDataVersion=1 is required to create a meet link
        event = service.events().insert(
            calendarId='primary', 
            body=event, 
            conferenceDataVersion=1
        ).execute()
        
        meet_link = event.get('hangoutLink', 'No link generated')
        return f"Meeting created: {event.get('htmlLink')}\nGoogle Meet Link: {meet_link}"
    except Exception as e:
        return f"Error creating meeting: {str(e)}"

if __name__ == "__main__":
    mcp.run()
