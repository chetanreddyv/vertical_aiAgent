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

# Manager Agent Models
class AgentSelection(str, Enum):
    EMAIL = "email"
    SQL = "sql"
    DRIVE = "drive"
    CALENDAR = "calendar"
    DOCS = "docs"
    TLDV = "tldv"
    GENERAL = "general"

class Step(BaseModel):
    agent: AgentSelection = Field(..., description="The specialist agent to perform this step.")
    instruction: str = Field(..., description="Precise, self-contained instruction for the agent. Must include all necessary context from previous steps.")
    reasoning: str = Field(..., description="Why this step is necessary and how it contributes to the final goal.")

class ExecutionPlan(BaseModel):
    steps: list[Step] = Field(..., description="Ordered list of steps to execute the user's request.")
    rewritten_intent: str = Field(..., description="A refined, self-contained summary of the user's request. e.g., 'Schedule a follow-up meeting with the client for next Tuesday at 10 AM'.")
    final_response_instruction: str = Field(..., description="Instruction on how to synthesize the final execution results into a response for the user.")

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")
    database: Optional[str] = Field(None, description="The specific database to query. Defaults to Salesforce if not specified.")

# -------- MCP Server Initializations --------

# MySQL
mysql_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["sql_server.py"],
    env={
        "DB_HOST": os.getenv("DB_HOST", "localhost"),
        "DB_PORT": os.getenv("DB_PORT", "3306"),
        "DB_USER": os.getenv("DB_USER", "root"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "CUSTOM_SQL_CONTEXT": os.getenv("CUSTOM_SQL_CONTEXT", ""),
    },
    timeout=60,
)

# Custom Calendar MCP
calendar_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["calendar_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

# Custom Drive MCP
drive_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
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
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["rag_server.py"],
    env={
        "PG_HOST": os.getenv("PG_HOST", "localhost"),
        "PG_PORT": os.getenv("PG_PORT", "5434"),
        "PG_USER": os.getenv("PG_USER", "chetan"),
        "PG_PASSWORD": os.getenv("PG_PASSWORD", ""),
        "PG_DB": os.getenv("PG_DB", "vectordb"),
        "PG_SCHEMA": os.getenv("PG_SCHEMA", "app_data"),
        "PG_TABLE_NAME": os.getenv("PG_TABLE_NAME", "meeting_embeddings"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "text-embedding-3-small"),
    }
)


manager_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=ExecutionPlan,
    system_prompt=(
        "You are the Manager Agent, an intelligent orchestrator for a multi-agent system.\n"
        "Your goal is to break down user requests into a sequence of actionable steps executed by specialist agents.\n\n"
        "AVAILABLE AGENTS:\n"
        "- 'tldv': SOURCE OF TRUTH for PAST meetings. Use for transcripts, summaries, 'what did X say', 'meeting from last week'.\n"
        "   - Capabilities: Advanced hybrid search with date/speaker/similarity filtering.\n"
        "   - IMPORTANT: Extract temporal context (dates, 'last week') and speaker names from user queries.\n"
        "   - Pass contextual hints in instructions (e.g., 'Search for budget discussions from last week').\n"
        "- 'calendar': Future scheduling, availability, and specific event metadata (time/location/participants).\n"
        "   - Capabilities: Create/Update events, Check availability.\n"
        "- 'email': Sending and reading emails (Gmail).\n"
        "- 'sql': Querying the business database (Salesforce data).\n"
        "- 'drive': File management (Drive) and reading file content.\n"
        "- 'docs': Creating and editing Google Docs.\n"
        "- 'general': Simple conversational, logic, or math tasks not requiring tools.\n\n"
        "RULES:\n"
        "1. ANALYZE context: You have access to the full conversation history. Resolve references like 'that meeting' or 'the file' based on previous turns.\n"
        "2. CHAIN steps: If a task requires output from one agent to be used by another (e.g., 'email the meeting summary'), create sequential steps.\n"
        "   - Step 1: tldv ('Summarize the meeting...')\n"
        "   - Step 2: email ('Send an email... Content: [Wait for Step 1 result]')\n"
        "   - Note: You define the plan UPFRONT. In the 'instruction' for Step 2, clearly state that it depends on the result of Step 1.\n"
        "3. TLDV vs CALENDAR: \n"
        "   - If the user asks about CONTENT (what was said, summaries, topics) -> TLDV.\n"
        "   - If the user asks about LOGISTICS (when is it, invite who) -> CALENDAR.\n"
        "   - If ambiguous (e.g., 'meeting yesterday'), defaulting to TLDV is usually safer for content queries.\n"
        "4. TLDV INSTRUCTIONS: Include temporal and speaker context explicitly:\n"
        "   - User: 'What did Sarah say last week?' -> Step instruction: 'Search for Sarah's comments from the past week'\n"
        "   - User: 'Meeting about budget yesterday' -> Step instruction: 'Find budget discussions from yesterday'\n"
        "   - This helps the TLDV agent apply appropriate filters for better accuracy."
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
        "You are a TLDV Meeting Notetaker assistant with advanced search capabilities.\n\n"
        "## Core Capabilities\n"
        "- Search across all past meeting transcripts using hybrid semantic + keyword search\n"
        "- Filter by date ranges, speakers, and specific meetings\n"
        "- Retrieve the most relevant context with high accuracy\n\n"
        "## Search Tool Usage\n"
        "You have access to 'search_meetings' with advanced parameters:\n\n"
        "**Key Parameters:**\n"
        "- `query` (required): Natural language question or topic\n"
        "- `min_similarity` (default: 0.2): Relevance threshold. Use 0.2 for broad searches.\n"
        "- `include_context` (default: False): Set to `True` for complex queries where understanding the flow of conversation (previous/next turns) is critical.\n\n"
        "**Filtering Parameters:**\n"
        "- `start_date` / `end_date`: ISO format (YYYY-MM-DD)\n"
        "- `speaker`: Partial match (e.g. 'Sarah')\n"
        "- `meeting_id`: Specific meeting scope\n\n"
        "**Quality Parameters:**\n"
        "- `deduplicate` (default: True): Prevents info overload by limiting chunks per meeting\n"
        "- `max_results_per_meeting` (default: 3): Max chunks from same meeting\n\n"
        "## Best Practices\n"
        "1. **Use filters proactively**: If user mentions time ('last month'), dates, or speakers, USE the filters.\n"
        "2. **Context Matters**: If the user asks a complex question like 'How did they reach that conclusion?', use `search_meetings(..., include_context=True)`.\n"
        "3. **Keyword-rich queries**: For decisions/action items, use keywords like 'decided', 'action item', 'agreed' in your query.\n"
        "4. **Adjust similarity threshold**: \n"
        "   - For specific facts/names: `min_similarity=0.4`\n"
        "   - Default is `0.2`. RRF fusion is automatic.\n"
        "5. **Interpret results carefully**:\n"
        "   - **Score 0.2 - 0.35**: Potential match. Review content carefully.\n"
        "   - **Score > 0.35**: High confidence match.\n"
        "   - **Keyword Rank > 0.1**: Strong keyword match.\n"
        "   - Use `citation_label` for referencing sources.\n"
        "   - Use `metadata` (date, speaker, meeting title) to add context\n"
        "6. **Citation**: Always cite which meeting(s) information came from using metadata.\n"
        "7. **Multi-step searches**: If first search is too narrow, try broader query or lower threshold.\n\n"
        "## Response Format\n"
        "When presenting results:\n"
        "- Start with direct answer\n"
        "- Cite source: 'According to the [meeting title] on [date]...'\n"
        "- If multiple meetings: Group by meeting or chronologically\n"
        "- Include speaker attribution when relevant\n"
        "- If low confidence (similarity < 0.6): 'Based on possibly related discussions...'\n\n"
        "## Important Distinctions\n"
        "- You handle PAST meeting content (transcripts, what was said)\n"
        "- For FUTURE meetings (scheduling, invites) → defer to Calendar agent\n"
        "- For meeting recordings/files → defer to Drive agent\n"
    ),
    mcp_servers=[rag_mcp]
)

sql_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=sql,
    system_prompt="PLACEHOLDER", 
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
        # We now fetch ALL schemas (utils.py filters the system ones)
        schema_info_result = await mysql_mcp.direct_call_tool(
            name="schema_info", 
            args={} 
        )
        if schema_info_result.get("success"):
            formatted_schema = format_schema_rows(schema_info_result["schema"])
    except Exception as e:
        logger.error(f"Error fetching schema: {e}", exc_info=True)

    # Update SQL agent prompt
    sql_agent.system_prompt = f"""You are an Expert MySQL Database Analyst and Data Scientist. 
Your goal is to answer user questions by generating precise, efficient, and read-only SQL queries.

## 🌍 Database Environment
You are connected to a MySQL instance containing multiple databases.
- **PRIMARY DATABASE**: `Salesforce` (Contains core business data: Leads, Opportunities, Accounts, Contacts).
- **Other Databases**: You have access to other user databases listed in the schema below.
- **System Databases**: `information_schema` is available for metadata queries (tables, columns, etc.).

## 🧠 Capabilities & Rules
1. **Target the Right Database**:
   - Usage: If the query is about business data, default to `Salesforce`. 
   - Usage: If the user explicitly mentions another DB (e.g., "in the leads_db"), use that.
   - **CRITICAL**: You must set the `database` field in your output to match the target.

2. **Cross-Database Queries**:
   - You can JOIN tables across databases using `database.table` syntax.
   - Example: `SELECT a.Name FROM Salesforce.Account a JOIN leads_db.ExternalLeads e ON ...`

3. **Schema & Metadata**:
   - To find tables/columns if you are unsure: Query `information_schema`.
   - List all tables: `SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema')`

4. **Safety & Performance**:
   - **READ ONLY**: Only SELECT, SHOW, DESCRIBE, EXPLAIN are allowed.
   - **LIMIT**: Always use `LIMIT 100` (or less) unless specific aggregation is requested.
   - **NO WILD**: Avoid `SELECT *` on large tables if not needed. Select specific columns.

## 🛠️ Output Format
You must return a structured JSON object (handled by the tool):
- `sqlquery`: The valid MySQL query.
- `explanation`: A concise, professional explanation of what the query retrieves.
- `database`: The target database name (e.g., 'Salesforce', 'mysql' for system info).

## 📚 Schema Context
{formatted_schema}

## 📝 Business Context & Definitions
{custom_sql_context}

## 💡 Examples
- "List all databases" -> `SHOW DATABASES` (database='mysql')
- "Show all tables" -> `SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES...` (database='mysql')
- "Find the top 5 opportunities" -> `SELECT Name, Amount FROM Opportunity ORDER BY Amount DESC LIMIT 5` (database='Salesforce')
- "Join Salesforce Accounts with External Leads" -> `SELECT ... FROM Salesforce.Account a JOIN other_db.Leads l ...` (database='Salesforce')
"""

    return formatted_schema

