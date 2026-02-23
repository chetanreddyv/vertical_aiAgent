from fastmcp import FastMCP
import os.path
import datetime
import uuid
from googleapiclient.discovery import build
from dotenv import load_dotenv
import os
import sys
import os.path as _osp
sys.path.insert(0, _osp.dirname(_osp.dirname(_osp.abspath(__file__))))
from google_auth_helper import get_google_creds
import logging

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("calendar_server")

from typing import List

# Scopes are managed centrally in google_auth_helper.py

mcp = FastMCP("Google Calendar Server")

WATERMARK = "\n\n-By JerseySTEM Cowork Agent"

def get_calendar_service():
    """Get authenticated Google Calendar service using shared credentials."""
    creds = get_google_creds()
    return build('calendar', 'v3', credentials=creds)

@mcp.tool()
def list_events(max_results: int = 10) -> str:
    """List upcoming calendar events.
    
    Args:
        max_results: Maximum number of events to return (default: 10)
    """
    logger.info(f"🛠️ Tool Called: list_events(max_results={max_results})")
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
            
        logger.info(f"✅ Tool Complete: list_events")
        return result
    except Exception as e:
        logger.error(f"❌ Tool Error: list_events - {str(e)}")
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
    logger.info(f"🛠️ Tool Called: create_event(summary='{summary}', start='{start_time}')")
    try:
        service = get_calendar_service()
        
        # Appending watermark
        description = f"{description}{WATERMARK}"

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
        logger.info(f"✅ Tool Complete: create_event")
        return f"Event created: {event.get('htmlLink')}"
    except Exception as e:
        logger.error(f"❌ Tool Error: create_event - {str(e)}")
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
        
        # Appending watermark
        description = f"{description}{WATERMARK}"

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
