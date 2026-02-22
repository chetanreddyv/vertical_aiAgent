from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStdio
from typing import Optional
from dataclasses import dataclass
import os
import sys
import json
import logging
import time
from dotenv import load_dotenv
from utils import format_schema_rows
from langfuse import observe

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Required environment variables
required_vars = [
    "EMAIL_PASSWORD", "EMAIL_ADDRESS", "DB_HOST", "DB_USER", 
    "DB_PASSWORD", "GEMINI_API_KEY", 
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "TLDV_API_KEY"
]
for var in required_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")

# -------- Data Models --------

@dataclass
class SupervisorDeps:
    """Dependencies for the supervisor agent, includes SQL config."""
    schema_text: str
    business_context: str
    approved_databases: list[str]
    default_database: str = "Salesforce"

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")
    database: Optional[str] = Field(None, description="The specific database to query. Defaults to Salesforce if not specified.")

class CitableResult(BaseModel):
    answer: str = Field(..., description="The factual answer based on the retrieved information.")
    sources: list[str] = Field(default_factory=list, description="List of source labels or citations used to form the answer.")

# -------- MCP Server Initializations --------

mysql_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/sql_server.py"],
    env={
        "DB_HOST": os.getenv("DB_HOST", "localhost"),
        "DB_PORT": os.getenv("DB_PORT", "3306"),
        "DB_USER": os.getenv("DB_USER", "root"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "CUSTOM_SQL_CONTEXT": os.getenv("CUSTOM_SQL_CONTEXT", ""),
    },
    timeout=60,
)

email_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/email_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

calendar_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/calendar_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

drive_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/drive_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

rag_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/rag_server.py"],
    env={
        "PINECONE_API_KEY": os.getenv("PINECONE_API_KEY", ""),
        "PINECONE_INDEX_NAME": os.getenv("PINECONE_INDEX_NAME", "meeting-transcripts-v3"),
        "PINECONE_MODEL": os.getenv("PINECONE_MODEL", "gemini-embedding-001"),
        "PINECONE_HOST": os.getenv("PINECONE_HOST", ""),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "COHERE_API_KEY": os.getenv("COHERE_API_KEY", ""),
        "COHERE_API": os.getenv("COHERE_API", ""),
    },
    timeout=120,
)

jira_mcp = MCPServerStdio(
    sys.executable,
    args=["mcp_servers/jira_server.py"],
    env={
        "JIRA_PAT": os.getenv("JIRA_PAT", ""),
        "JIRA_URL": os.getenv("JIRA_URL", ""),
    }
)

# -------- Specialist Agents (internal, called by supervisor tools) --------

email_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Gmail automation assistant with access to Google Workspace tools.\n\n"
        "Capabilities:\n"
        "- Search and retrieve emails with filters (sender, subject, date range)\n"
        "- Read email content and extract information\n\n"
        "Guidelines:\n"
        "- Use list_messages and get_message tools to search and read emails.\n"
        "- Provide clear summaries of email content.\n"
        "- CRITICAL: If asked to read/reply, FIRST search for the email ID."
    ),
    mcp_servers=[email_mcp]
)

drive_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Google Drive file management assistant.\n\n"
        "Capabilities:\n"
        "- Search files/folders metadata using 'search_files'\n"
        "- Read content of specific files via 'get_file_content'\n"
        "- List files in folders\n"
        "- Get file metadata\n\n"
        "Guidelines:\n"
        "- You ONLY perform read operations (search, list, get content).\n"
        "- Provide clear file listings with names, types, and sizes.\n"
    ),
    mcp_servers=[drive_mcp]
)

calendar_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Google Calendar assistant.\n\n"
        "Capabilities:\n"
        "- Search for existing events via 'list_events'\n"
        "- Check availability and find free time slots\n\n"
        "Guidelines:\n"
        "- Parse natural language dates and times accurately\n"
        "- Provide clear summaries of scheduled events\n"
    ),
    mcp_servers=[calendar_mcp]
)

jira_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Jira project management assistant.\n\n"
        "Capabilities:\n"
        "- Search for issues using JQL or natural language via 'jira_search'\n"
        "- Get detailed issue information via 'jira_get_issue'\n"
        "- Get comments via 'jira_get_comments'\n"
        "- List projects via 'jira_list_projects'\n"
        "- Get available transitions via 'jira_get_transitions'\n\n"
        "Guidelines:\n"
        "- When searching, provide key details: Key, Summary, Status, Assignee, Priority\n"
    ),
    mcp_servers=[jira_mcp]
)

sql_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    output_type=sql,
    deps_type=SupervisorDeps,
    mcp_servers=[mysql_mcp]
)

@sql_agent.system_prompt
def sql_system_prompt(ctx: RunContext[SupervisorDeps]) -> str:
    """Generate system prompt with injected schema and configuration at runtime."""
    deps = ctx.deps
    return f"""You are an Expert MySQL Database Analyst and Data Scientist.
Your goal is to answer user questions by generating precise, efficient, and read-only SQL queries.

## 🌍 Database Environment
You are connected to a MySQL instance containing multiple databases.
- **PRIMARY DATABASE**: `{deps.default_database}` (Contains core business data: Leads, Opportunities, Accounts, Contacts)
- **APPROVED DATABASES**: {', '.join(f'`{db}`' for db in deps.approved_databases)}
- **System Databases**: `information_schema` available for metadata queries

## 🧠 Capabilities & Rules
1. **Target the Right Database**:
   - Default to `{deps.default_database}` for business data queries
   - Use other approved databases if explicitly mentioned by the user
   - **CRITICAL**: You MUST set the `database` field in your output to match the target database

2. **Cross-Database Queries**:
   - You can JOIN tables across databases using `database.table` syntax
   - Example: `SELECT a.Name FROM {deps.default_database}.Account a JOIN other_db.Leads l ON a.Id = l.AccountId`

3. **Schema & Metadata**:
   - The complete database schema is provided below in the "Database Schema" section
   - Use the schema to understand available tables and columns
   - For dynamic exploration, query `information_schema`:
     * List tables: `SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema')`
     * Describe table: `DESCRIBE database_name.table_name`

4. **Safety & Performance**:
   - **READ ONLY**: Only SELECT, SHOW, DESCRIBE, EXPLAIN queries are allowed
   - **RESERVED KEYWORDS**: Always use backticks for table and column names to avoid conflicts with reserved keywords (e.g., use `` `Lead` ``, `` `Order` ``).
   - **LIMIT RESULTS**: Always use `LIMIT 100` (or less) unless specific aggregation is requested
   - **AVOID WILDCARDS**: Don't use `SELECT *` on large tables - select specific columns instead

## 🛠️ Output Format
You must return a structured JSON object with these fields:
- `sqlquery`: The valid MySQL query to execute
- `explanation`: A concise, professional explanation of what the query retrieves
- `database`: The target database name (e.g., '{deps.default_database}', etc.)

## 📚 Database Schema
{deps.schema_text}

## 📝 Business Context & Definitions
{deps.business_context if deps.business_context else 'No additional business context provided.'}

## 💡 Examples
- "List all databases" → Query: `SHOW DATABASES`, Database: 'mysql'
- "Top 5 opportunities" → Query: `SELECT Name, Amount FROM Opportunity ORDER BY Amount DESC LIMIT 5`, Database: '{deps.default_database}'
- "Get top 3 leads" → Query: `SELECT FirstName, LastName FROM `Lead` ORDER BY CreatedDate DESC LIMIT 3`, Database: '{deps.default_database}'
- "Count items in Salesforce" → Query: `SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{deps.default_database}'`, Database: 'information_schema'

"""

general_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    output_type=CitableResult,
    system_prompt=(
        "You are a helpful, knowledgeable AI assistant and the primary source of truth for the organization.\n\n"
        "Capabilities:\n"
        "- Answer general questions conversationally.\n"
        "- Search across all past meeting transcripts using semantic vector search ('search_meetings').\n"
        "- Search across all Drive documents (PDFs, Docs, Text) in the knowledge base ('search_documents').\n"
        "- Synthesize information from other agents when provided.\n\n"
        "Guidelines:\n"
        "1. **RAG Search Usage**:\n"
        "   - For meetings: Use 'search_meetings'. Filter by date/speaker when possible.\n"
        "   - For documents: Use 'search_documents'.\n"
        "   - Interpret results carefully. Use metadata to add context (date, speakers, titles).\n"
        "2. **Sources & Citations**:\n"
        "   - Populate the `sources` field in your output with the `citation_label` of every document/meeting used.\n"
        "   - Do not hallucinate sources. Do not put citations inline in the answer text, just list them in the `sources` list.\n\n"
        "3. **Synthesis**:\n"
        "   - If you are provided with `Execution Results` from other agents, focus on that factual content to answer."
    ),
    mcp_servers=[rag_mcp]
)

# -------- Supervisor Agent --------

CONFIRMATION_MARKER = "__confirmation_required__"

supervisor_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    deps_type=SupervisorDeps,
    system_prompt=(
        "You are the Supervisor Agent, an intelligent orchestrator for a multi-agent system.\n"
        "You have access to specialist tools that you call directly to fulfill user requests.\n\n"
        "## AVAILABLE TOOLS\n\n"
        "### Read Tools (execute immediately)\n"
        "- `search_knowledge`: Search past meetings, transcripts, and Drive documents. SOURCE OF TRUTH for organizational knowledge.\n"
        "- `query_database`: Query the business database (Salesforce data) using natural language.\n"
        "- `search_emails`: Search and read emails.\n"
        "- `search_calendar`: Search calendar events and check availability.\n"
        "- `search_drive`: Search and read files in Google Drive.\n"
        "- `search_jira`: Search Jira issues and get issue details.\n\n"
        "### Write Tools (require user confirmation before executing)\n"
        "- `send_email`: Send an email via Gmail.\n"
        "- `create_calendar_event`: Create a calendar event or meeting.\n"
        "- `create_jira_issue`: Create a new Jira issue.\n"
        "- `update_jira_issue`: Update an existing Jira issue.\n"
        "- `add_jira_comment`: Add a comment to a Jira issue.\n"
        "- `transition_jira_issue`: Change the status of a Jira issue.\n"
        "- `create_drive_folder`: Create a new folder in Google Drive.\n"
        "- `upload_to_drive`: Upload a file to Google Drive.\n\n"
        "## RULES\n"
        "1. **CONTEXT**: You have access to the full conversation history. Resolve references like 'that meeting' or 'the file' based on previous turns.\n"
        "2. **CHAINING**: If a task requires output from one tool to feed into another (e.g., 'email the meeting summary'), call them in sequence.\n"
        "3. **ROUTING**:\n"
        "   - CONTENT questions (what was said, summaries, topics) → `search_knowledge`\n"
        "   - LOGISTICS (when is it, invite who, schedule) → `search_calendar` or `create_calendar_event`\n"
        "   - DATA/METRICS (how many leads, revenue, etc.) → `query_database`\n"
        "4. **TEMPORAL**: Extract temporal context (dates, 'last week') and pass it in your tool instructions.\n"
        "5. **WRITE TOOLS**: Write tools will return a confirmation prompt. Relay it to the user as-is — the system handles the confirmation flow.\n"
        "6. **CONVERSATIONAL**: For simple greetings or questions that don't need any tools, respond directly.\n"
    )
)

# -------- Read Tools (execute immediately) --------

@supervisor_agent.tool
async def search_knowledge(ctx: RunContext[SupervisorDeps], query: str) -> str:
    """Search meeting transcripts and Drive documents for organizational knowledge. 
    Use for questions about what was discussed, meeting content, company documents, policies, etc.
    Pass temporal hints in the query (e.g., 'budget discussion from last week')."""
    logger.info(f"🔍 search_knowledge: {query[:80]}...")
    from pydantic_ai import UsageLimits
    result = await general_agent.run(query, usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5))
    output = result.output
    response = output.answer
    if output.sources:
        response += "\n\nSources:\n- " + "\n- ".join(output.sources)
    return response

@supervisor_agent.tool
async def query_database(ctx: RunContext[SupervisorDeps], question: str) -> str:
    """Query the business database (Salesforce) using natural language.
    Use for data questions about leads, opportunities, accounts, contacts, revenue, etc."""
    logger.info(f"🗄️ query_database: {question[:80]}...")
    from pydantic_ai import UsageLimits
    from utils import format_query_results
    
    result = await sql_agent.run(
        question, 
        deps=ctx.deps,
        usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5)
    )
    
    query_str = result.output.sqlquery
    explanation = result.output.explanation or "SQL Query"
    database = result.output.database or ctx.deps.default_database
    
    # Execute the query via MCP
    query_result = await mysql_mcp.direct_call_tool(
        name="execute_query",
        args={"query": query_str, "database": database, "read_only": True}
    )
    
    formatted = format_query_results(query_result)
    return f"**Query**: `{query_str}`\n**Explanation**: {explanation}\n\n**Results**:\n{formatted}"

@supervisor_agent.tool
async def search_emails(ctx: RunContext[SupervisorDeps], instruction: str) -> str:
    """Search and read emails. Use for questions about emails, messages, inbox content.
    Include search filters in the instruction (sender, subject, date range)."""
    logger.info(f"📧 search_emails: {instruction[:80]}...")
    from pydantic_ai import UsageLimits
    result = await email_agent.run(instruction, usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5))
    return str(result.output)

@supervisor_agent.tool
async def search_calendar(ctx: RunContext[SupervisorDeps], instruction: str) -> str:
    """Search calendar events and check availability. Use for questions about upcoming meetings, schedules, free time."""
    logger.info(f"📅 search_calendar: {instruction[:80]}...")
    from pydantic_ai import UsageLimits
    result = await calendar_agent.run(instruction, usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5))
    return str(result.output)

@supervisor_agent.tool
async def search_drive(ctx: RunContext[SupervisorDeps], instruction: str) -> str:
    """Search and read files in Google Drive. Use for file management queries — listing, metadata, file content.
    For KNOWLEDGE retrieval from documents, prefer search_knowledge instead."""
    logger.info(f"📁 search_drive: {instruction[:80]}...")
    from pydantic_ai import UsageLimits
    result = await drive_agent.run(instruction, usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5))
    return str(result.output)

@supervisor_agent.tool
async def search_jira(ctx: RunContext[SupervisorDeps], instruction: str) -> str:
    """Search Jira issues, get issue details, list projects. Use for read-only Jira queries.
    For creating/updating issues, use the write tools instead."""
    logger.info(f"🎫 search_jira: {instruction[:80]}...")
    from pydantic_ai import UsageLimits
    result = await jira_agent.run(instruction, usage_limits=UsageLimits(request_limit=10, tool_calls_limit=5))
    return str(result.output)

# -------- Write Tools (HITL — return confirmation payload) --------

def _confirmation_payload(action: str, details: dict, preview: str) -> str:
    """Create a standardized confirmation payload for write actions."""
    return json.dumps({
        CONFIRMATION_MARKER: True,
        "action": action,
        "details": details,
        "preview": preview,
    })

@supervisor_agent.tool
async def send_email(ctx: RunContext[SupervisorDeps], to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> str:
    """Send an email via Gmail. This requires user confirmation before sending.
    Provide the recipient, subject, and body content."""
    logger.info(f"✉️ send_email (draft): to={to}, subject={subject[:50]}...")
    preview = f"**To**: {to}\n"
    if cc:
        preview += f"**CC**: {cc}\n"
    if bcc:
        preview += f"**BCC**: {bcc}\n"
    preview += f"**Subject**: {subject}\n\n{body}"
    
    return _confirmation_payload("send_email", {
        "to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc
    }, preview)

@supervisor_agent.tool
async def create_calendar_event(
    ctx: RunContext[SupervisorDeps], 
    summary: str, start_time: str, end_time: str, 
    description: str = "", attendees: str = "", is_meeting: bool = False
) -> str:
    """Create a calendar event or Google Meet meeting. Requires user confirmation.
    Use ISO format for times (e.g., '2024-03-15T10:00:00'). 
    For meetings with video link, set is_meeting=True. Attendees as comma-separated emails."""
    logger.info(f"📅 create_calendar_event (draft): {summary}")
    preview = f"**Event**: {summary}\n**Start**: {start_time}\n**End**: {end_time}\n"
    if description:
        preview += f"**Description**: {description}\n"
    if attendees:
        preview += f"**Attendees**: {attendees}\n"
    if is_meeting:
        preview += "**Type**: Google Meet Video Conference\n"
    
    return _confirmation_payload("create_calendar_event", {
        "summary": summary, "start_time": start_time, "end_time": end_time,
        "description": description, "attendees": attendees, "is_meeting": is_meeting
    }, preview)

@supervisor_agent.tool
async def create_jira_issue(
    ctx: RunContext[SupervisorDeps],
    project_key: str, summary: str, description: str,
    issue_type: str = "Task", priority: str = "", assignee: str = ""
) -> str:
    """Create a new Jira issue. Requires user confirmation.
    Provide project key, summary, and description at minimum."""
    logger.info(f"🎫 create_jira_issue (draft): {project_key} - {summary[:50]}")
    preview = f"**Project**: {project_key}\n**Type**: {issue_type}\n**Summary**: {summary}\n**Description**: {description}\n"
    if priority:
        preview += f"**Priority**: {priority}\n"
    if assignee:
        preview += f"**Assignee**: {assignee}\n"
    
    return _confirmation_payload("create_jira_issue", {
        "project_key": project_key, "summary": summary, "description": description,
        "issue_type": issue_type, "priority": priority, "assignee": assignee
    }, preview)

@supervisor_agent.tool
async def update_jira_issue(
    ctx: RunContext[SupervisorDeps],
    issue_key: str, summary: str = "", description: str = "",
    priority: str = "", assignee: str = ""
) -> str:
    """Update an existing Jira issue. Requires user confirmation.
    Provide the issue key and the fields to update."""
    logger.info(f"🎫 update_jira_issue (draft): {issue_key}")
    preview = f"**Issue**: {issue_key}\n"
    if summary:
        preview += f"**New Summary**: {summary}\n"
    if description:
        preview += f"**New Description**: {description}\n"
    if priority:
        preview += f"**New Priority**: {priority}\n"
    if assignee:
        preview += f"**New Assignee**: {assignee}\n"
    
    return _confirmation_payload("update_jira_issue", {
        "issue_key": issue_key, "summary": summary, "description": description,
        "priority": priority, "assignee": assignee
    }, preview)

@supervisor_agent.tool
async def add_jira_comment(ctx: RunContext[SupervisorDeps], issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue. Requires user confirmation."""
    logger.info(f"🎫 add_jira_comment (draft): {issue_key}")
    preview = f"**Issue**: {issue_key}\n**Comment**: {comment}"
    return _confirmation_payload("add_jira_comment", {
        "issue_key": issue_key, "comment_body": comment
    }, preview)

@supervisor_agent.tool
async def transition_jira_issue(ctx: RunContext[SupervisorDeps], issue_key: str, transition_name: str, comment: str = "") -> str:
    """Change the status of a Jira issue (e.g., 'In Progress', 'Done'). Requires user confirmation."""
    logger.info(f"🎫 transition_jira_issue (draft): {issue_key} → {transition_name}")
    preview = f"**Issue**: {issue_key}\n**New Status**: {transition_name}\n"
    if comment:
        preview += f"**Comment**: {comment}\n"
    return _confirmation_payload("transition_jira_issue", {
        "issue_key": issue_key, "transition_name": transition_name, "comment": comment
    }, preview)

@supervisor_agent.tool
async def create_drive_folder(ctx: RunContext[SupervisorDeps], name: str, parent_folder_id: str = "") -> str:
    """Create a new folder in Google Drive. Requires user confirmation."""
    logger.info(f"📁 create_drive_folder (draft): {name}")
    preview = f"**Folder Name**: {name}\n"
    if parent_folder_id:
        preview += f"**Parent Folder ID**: {parent_folder_id}\n"
    return _confirmation_payload("create_drive_folder", {
        "name": name, "parent_folder_id": parent_folder_id
    }, preview)

@supervisor_agent.tool
async def upload_to_drive(ctx: RunContext[SupervisorDeps], file_path: str, folder_id: str = "", file_name: str = "") -> str:
    """Upload a file to Google Drive. Requires user confirmation."""
    logger.info(f"📁 upload_to_drive (draft): {file_path}")
    preview = f"**File**: {file_path}\n"
    if folder_id:
        preview += f"**Destination Folder ID**: {folder_id}\n"
    if file_name:
        preview += f"**Drive Name**: {file_name}\n"
    return _confirmation_payload("upload_to_drive", {
        "file_path": file_path, "folder_id": folder_id, "file_name": file_name
    }, preview)

# -------- Confirmation Executor --------

async def execute_confirmed_action(action: str, details: dict) -> str:
    """Execute a confirmed write action by calling the appropriate MCP tool directly."""
    logger.info(f"✅ Executing confirmed action: {action}")
    
    try:
        if action == "send_email":
            result = await email_mcp.direct_call_tool(
                name="send_email",
                args={
                    "to": details["to"],
                    "subject": details["subject"],
                    "body": details["body"],
                    "cc": details.get("cc", ""),
                    "bcc": details.get("bcc", ""),
                }
            )
            return f"✅ Email sent successfully to {details['to']}"
        
        elif action == "create_calendar_event":
            if details.get("is_meeting"):
                attendees = [e.strip() for e in details.get("attendees", "").split(",") if e.strip()]
                result = await calendar_mcp.direct_call_tool(
                    name="create_meeting",
                    args={
                        "summary": details["summary"],
                        "start_time": details["start_time"],
                        "end_time": details["end_time"],
                        "attendees": attendees,
                        "description": details.get("description", ""),
                    }
                )
            else:
                result = await calendar_mcp.direct_call_tool(
                    name="create_event",
                    args={
                        "summary": details["summary"],
                        "start_time": details["start_time"],
                        "end_time": details["end_time"],
                        "description": details.get("description", ""),
                    }
                )
            return f"✅ Calendar event created: {details['summary']}"
        
        elif action == "create_jira_issue":
            args = {
                "project_key": details["project_key"],
                "summary": details["summary"],
                "description": details["description"],
                "issue_type": details.get("issue_type", "Task"),
            }
            if details.get("priority"):
                args["priority"] = details["priority"]
            if details.get("assignee"):
                args["assignee"] = details["assignee"]
            result = await jira_mcp.direct_call_tool(name="jira_create_issue", args=args)
            return f"✅ Jira issue created in {details['project_key']}: {details['summary']}"
        
        elif action == "update_jira_issue":
            args = {"issue_key": details["issue_key"]}
            if details.get("summary"):
                args["summary"] = details["summary"]
            if details.get("description"):
                args["description"] = details["description"]
            if details.get("priority"):
                args["priority"] = details["priority"]
            if details.get("assignee"):
                args["assignee"] = details["assignee"]
            result = await jira_mcp.direct_call_tool(name="jira_update_issue", args=args)
            return f"✅ Jira issue {details['issue_key']} updated"
        
        elif action == "add_jira_comment":
            result = await jira_mcp.direct_call_tool(
                name="jira_add_comment",
                args={"issue_key": details["issue_key"], "comment_body": details["comment_body"]}
            )
            return f"✅ Comment added to {details['issue_key']}"
        
        elif action == "transition_jira_issue":
            args = {
                "issue_key": details["issue_key"],
                "transition_name": details["transition_name"],
            }
            if details.get("comment"):
                args["comment"] = details["comment"]
            result = await jira_mcp.direct_call_tool(name="jira_transition_issue", args=args)
            return f"✅ {details['issue_key']} transitioned to {details['transition_name']}"
        
        elif action == "create_drive_folder":
            args = {"name": details["name"]}
            if details.get("parent_folder_id"):
                args["parent_folder_id"] = details["parent_folder_id"]
            result = await drive_mcp.direct_call_tool(name="create_folder", args=args)
            return f"✅ Drive folder '{details['name']}' created"
        
        elif action == "upload_to_drive":
            args = {"file_path": details["file_path"]}
            if details.get("folder_id"):
                args["folder_id"] = details["folder_id"]
            if details.get("file_name"):
                args["file_name"] = details["file_name"]
            result = await drive_mcp.direct_call_tool(name="upload_file", args=args)
            return f"✅ File uploaded to Drive"
        
        else:
            return f"❌ Unknown action: {action}"
    
    except Exception as e:
        logger.error(f"❌ Error executing {action}: {e}", exc_info=True)
        return f"❌ Error executing {action}: {str(e)}"

# -------- Initialization --------

@observe()
async def initialize_agents() -> SupervisorDeps:
    """Initialize agents and return supervisor dependencies."""
    logger.info("🎬 Starting Agents Initialization...")
    init_start = time.time()
    
    # Load SQL context from file
    sql_context_file = "sql_context.md"
    custom_sql_context = ""
    try:
        if os.path.exists(sql_context_file):
            with open(sql_context_file, "r") as f:
                custom_sql_context = f.read()
            logger.info(f"📄 Loaded SQL context from {sql_context_file}")
    except Exception as e:
        logger.error(f"❌ Failed to read {sql_context_file}: {e}")
    
    # Fetch schema using format_schema_rows from utils.py
    formatted_schema = "Schema unavailable."
    try:
        schema_info_result = await mysql_mcp.direct_call_tool(
            name="schema_info", 
            args={} 
        )
        if schema_info_result.get("success"):
            formatted_schema = format_schema_rows(
                schema_info_result["schema"], 
                include_dbs=["Salesforce", "information_schema"], 
                lightweight=True
            )
            logger.info(f"📊 Schema loaded successfully ({len(schema_info_result['schema'])} columns)")
    except Exception as e:
        logger.error(f"❌ Error fetching schema: {e}", exc_info=True)
    
    deps = SupervisorDeps(
        schema_text=formatted_schema,
        business_context=custom_sql_context,
        approved_databases=["Salesforce", "mysql", "information_schema"],
        default_database="Salesforce"
    )
    
    init_duration = time.time() - init_start
    logger.info(f"✅ Agents Initialization Complete ({init_duration:.2f}s)")
    
    return deps
