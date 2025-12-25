from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from enum import Enum
from typing import Optional
import os
import logging
from dotenv import load_dotenv
from utils import format_schema_rows

# Configure logging
logger = logging.getLogger(__name__)

load_dotenv()

# Required environment variables including Google OAuth
required_vars = [
    "EMAIL_PASSWORD", "EMAIL_ADDRESS", "DB_HOST", "DB_USER", 
    "DB_PASSWORD", "OPENAI_API_KEY", 
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "TLDV_API_KEY"
]
for var in required_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        # We don't raise here to allow import, but initialization might fail

# Intent handling extended for Google Workspace services
class IntentType(str, Enum):
    EMAIL_ONLY = "email_only"
    SQL_ONLY = "sql_only"
    EMAIL_AND_SQL = "email_and_sql"
    DRIVE_ONLY = "drive_only"
    CALENDAR_ONLY = "calendar_only"
    DOCS_ONLY = "docs_only"
    TLDV_ONLY = "tldv_only"
    GENERAL = "general"

class ExecutionPlan(BaseModel):
    intent: IntentType
    rewritten_query: str = Field(..., description="Rewritten user query that is specific, actionable, and context-rich.")
    email_task: Optional[str]
    sql_task: Optional[str]
    drive_task: Optional[str] = None
    calendar_task: Optional[str] = None
    docs_task: Optional[str] = None
    tldv_task: Optional[str] = None

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")

# -------- MCP Server Initializations --------

# MySQL (unchanged)
mysql_mcp = MCPServerStdio(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    args=["sql_server.py"],
    env={
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_PORT": os.getenv("DB_PORT", "3306"),
        "DB_USER": os.getenv("DB_USER"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "CUSTOM_SQL_CONTEXT": os.getenv("CUSTOM_SQL_CONTEXT", ""),
    },
    timeout=60,
)


# Custom Calendar MCP
calendar_mcp = MCPServerStdio(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    args=["calendar_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

# Custom Drive MCP
drive_mcp = MCPServerStdio(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    args=["drive_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)


# TLDV MCP (Docker)
tldv_mcp = MCPServerStdio(
    "docker",
    args=[
        "run",
        "-i", 
        "--init", 
        "--rm", 
        "-e", 
        f"TLDV_API_KEY={os.getenv('TLDV_API_KEY')}", 
        "tldv-mcp-server"
    ]
)


# RAG MCP (Postgres Vector)
rag_mcp = MCPServerStdio(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    args=["rag_server.py"],
    env={
        "PG_HOST": os.getenv("PG_HOST", "localhost"),
        "PG_PORT": os.getenv("PG_PORT", "5435"),
        "PG_USER": os.getenv("PG_USER", "admin"),
        "PG_PASSWORD": os.getenv("PG_PASSWORD", "password"),
        "PG_DB": os.getenv("PG_DB", "vectordb"),
        "PG_SCHEMA": os.getenv("PG_SCHEMA", "app_data"),
        "PG_TABLE_NAME": os.getenv("PG_TABLE_NAME", "meeting_embeddings"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "text-embedding-3-small"),
    }
)


# -------- Agent Definitions (all use workspace MCP except SQL) -------

intent_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=ExecutionPlan,
    system_prompt=(
        "You are an intent classification and query refinement expert. Your goal is to understand the user's request, "
        "classify it into the correct intent, and rewrite the query to be precise and actionable for the specific agent.\n\n"
        "Classification Options:\n"
        "- 'email_only': Gmail operations (send, read, search emails)\n"
        "- 'sql_only': Database queries or data retrieval from MySQL\n"
        "- 'email_and_sql': Combined workflow (query database, then email results)\n"
        "- 'drive_only': Google Drive operations (upload, download, search files)\n"
        "- 'calendar_only': Google Calendar: Schedule/Create/Update events & Google Meet links\n"
        "- 'docs_only': Google Docs operations (create, edit documents)\n"
        "- 'tldv_only': TLDV: Retrieve transcripts, summaries, and highlights from PAST meetings\n"
        "- 'general': General questions or conversations not requiring tools\n\n"
        "Instructions:\n"
        "1. Analyze the user's input to determine the primary intent.\n"
        "2. Rewrite the user's query in the 'rewritten_query' field. This should be a clear, third-person statement of what the user wants (e.g., 'Search for the budget file in Drive' instead of 'find the budget file').\n"
        "3. Populate the specific task field corresponding to the chosen intent (e.g., 'email_task' for 'email_only').\n"
        "   - The task description in the specific field should be DETAILED and SELF-CONTAINED. \n"
        "   - Include all necessary context, parameters, and constraints.\n"
        "   - For 'email_and_sql', populate BOTH 'sql_task' and 'email_task'.\n"
        "   - Leave unrelated task fields null."
    )
)

email_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a Gmail automation assistant with access to Google Workspace tools.\n\n"
        "Capabilities:\n"
        "- Search and retrieve emails with filters (sender, subject, date range)\n"
        "- Send emails with rich formatting and attachments\n"
        "- Read email content and extract information\n"
        "- Manage labels and organize inbox\n\n"
        "Guidelines:\n"
        "- Always confirm before sending emails\n"
        "- Provide clear summaries of email content\n"
        "- Use appropriate filters to find relevant emails\n"
        "- Format responses in a clear, readable manner\n"
        "- CRITICAL: If asked to read, summarize, or reply to an email, YOU MUST FIRST search for the email to get its ID/Thread ID. Do not guess IDs."
    )
)

drive_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a Google Drive file management assistant.\n\n"
        "Capabilities:\n"
        "- Search files and folders using natural language (e.g., 'budget report') or Drive query syntax\n"
        "- Read content of Google Docs, Sheets, Slides, MS Office files, and text files\n"
        "- Upload and download files\n"
        "- Create folders to organize content\n"
        "- Get file metadata and details\n\n"
        "Guidelines:\n"
        "- Provide clear file listings with names, types, and sizes\n"
        "- Use descriptive folder structures\n"
        "- Report upload/download progress and success\n"
        "- When searching, try natural language first; the system handles the query translation\n"
        "- CRITICAL: If the user asks about the content of a file, YOU MUST FIRST search for the file to get its ID, and THEN read its content using that ID. Do not guess IDs."
    ),
    mcp_servers=[drive_mcp]
)

calendar_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a Google Calendar and video meeting scheduling assistant.\n\n"
        "Capabilities:\n"
        "- Create calendar events with dates, times, and descriptions\n"
        "- Create Google Meet video conferences (calendar events with video links)\n"
        "- Search for existing events\n"
        "- Update or cancel events\n"
        "- Check availability and find free time slots\n"
        "- Set reminders and notifications\n\n"
        "Guidelines:\n"
        "- Parse natural language dates and times accurately\n"
        "- Default to 1-hour duration if not specified\n"
        "- Use the user's local timezone\n"
        "- For video meetings, use the create_meeting tool which generates Meet links\n"
        "- For regular events without video, use the create_event tool\n"
        "- Confirm event details before creation\n"
        "- Provide clear summaries of scheduled events\n"
        "- CRITICAL: If asked to update or cancel an event, YOU MUST FIRST search for the event to get its ID. If asked to schedule, check for conflicts first.\n"
    ),
    mcp_servers=[calendar_mcp]
)

docs_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a Google Docs document assistant.\n\n"
        "Capabilities:\n"
        "- Create new documents with formatted content\n"
        "- Read and extract text from existing documents\n"
        "- Edit and update document content\n"
        "- Apply formatting (headings, lists, bold, italic)\n"
        "- Search document content\n\n"
        "Guidelines:\n"
        "- Use proper document structure with headings\n"
        "- Apply appropriate formatting for readability\n"
        "- Confirm before making major edits\n"
        "- Provide summaries of document content\n"
        "- CRITICAL: If asked to read or edit a document, YOU MUST FIRST search for the document to get its ID. Do not guess IDs."
    )
)

tldv_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a TLDV Meeting Notetaker assistant.\n\n"
        "Capabilities:\n"
        "- Search across all past meeting transcripts for specific topics, context, and decisions (RAG).\n\n"
        "Guidelines:\n"
        "- You have access to a semantic search tool 'search_meetings'.\n"
        "- ALWAYS use 'search_meetings' to find information about past meetings.\n"
        "- The search results include meeting content, metadata (speakers, time), and a 'meeting_id'.\n"
        "- Use the metadata to provide context (e.g., 'In the meeting on [date]...').\n"
        "- This tool is NOT for scheduling new meetings (use Calendar agent for that).\n"
    ),
    mcp_servers=[rag_mcp]
)

sql_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=sql,
    system_prompt="PLACEHOLDER",  # Updated later
    mcp_servers=[mysql_mcp]
)

general_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a helpful, knowledgeable AI assistant.\n\n"
        "When answering questions:\n"
        "- Provide accurate, concise information\n"
        "- Use examples when helpful\n"
        "- Admit when you don't know something\n"
        "- Be conversational and friendly\n\n"
        "Note: You don't have access to external tools for general queries. "
        "Inform users if they need specific functionality like email or database access."
    )
)

async def initialize_agents():
    """Initialize agents with dynamic configuration (schema, context)"""
    logger.info("Initializing agents...")
    
    # Load SQL context from file
    sql_context_file = "sql_context.md"
    custom_sql_context = ""
    try:
        if os.path.exists(sql_context_file):
            with open(sql_context_file, "r") as f:
                custom_sql_context = f.read()
            logger.info(f"Loaded SQL context from {sql_context_file}")
    except Exception as e:
        logger.error(f"Failed to read {sql_context_file}: {e}")
        


    # Fetch schema
    formatted_schema = "Schema unavailable."
    try:
        schema_info_result = await mysql_mcp.direct_call_tool(
            name="schema_info", 
            args={"database": "Salesforce"}
        )
        if schema_info_result.get("success"):
            formatted_schema = format_schema_rows(schema_info_result["schema"])
    except Exception as e:
        logger.error(f"Error fetching schema: {e}", exc_info=True)

    # Update SQL agent prompt
    sql_agent.system_prompt = f"""You are an expert SQL query generator. Your task is to convert natural language questions into valid SQL queries.

IMPORTANT GUIDELINES:
1. Generate only SELECT, SHOW, DESCRIBE, or EXPLAIN queries (read-only mode)
2. For listing tables, use: SHOW TABLES
3. For listing databases, use: SHOW DATABASES  
4. For table structure, use: DESCRIBE table_name or SHOW COLUMNS FROM table_name
5. Always use proper SQL syntax
6. Use appropriate JOINs when multiple tables are involved
7. Add WHERE clauses for filtering when relevant
8. Use LIMIT clauses to prevent overwhelming results (default LIMIT 100 unless specified)
9. Return column names that are meaningful
10. NEVER generate queries that start with anything other than SELECT, SHOW, DESCRIBE, DESC, or EXPLAIN

QUERY EXAMPLES FOR COMMON REQUESTS:
- "What tables are in the database?" → SHOW TABLES
- "List all databases" → SHOW DATABASES
- "What columns are in table X?" → SHOW COLUMNS FROM X
- "Show me data from table X" → SELECT * FROM X LIMIT 10

DB SCHEMA:
{formatted_schema}
CONTEXT:
{custom_sql_context}
Provide a valid query and brief explanation. Queries should be written using context and schema provided."""

    return formatted_schema

