from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStdio
from enum import Enum
from typing import Optional, Literal
from dataclasses import dataclass
import os
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

# Required environment variables including Google OAuth
required_vars = [
    "EMAIL_PASSWORD", "EMAIL_ADDRESS", "DB_HOST", "DB_USER", 
    "DB_PASSWORD", "GEMINI_API_KEY", 
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "TLDV_API_KEY"
]
for var in required_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        # We don't raise here to allow import, but initialization might fail

# SQL Dependencies for dependency injection
@dataclass
class SqlDeps:
    """Dependencies for SQL agent containing schema and database configuration."""
    schema_text: str  # Formatted database schema from format_schema_rows
    business_context: str  # Business rules and definitions from sql_context.md
    approved_databases: list[str]  # List of queryable databases
    default_database: str = "Salesforce"  # Default target database

# Manager Agent Models
class AgentSelection(str, Enum):
    EMAIL = "email"
    SQL = "sql"
    DRIVE = "drive"
    CALENDAR = "calendar"
    TLDV = "tldv"
    JIRA = "jira"
    GENERAL = "general"

class Step(BaseModel):
    id: str = Field(..., description="Unique identifier for this step (e.g., 's1', 's2').")
    agent: AgentSelection = Field(..., description="The specialist agent to perform this step.")
    instruction: str = Field(..., description="Precise, self-contained instruction for the agent.")
    depends_on: list[str] = Field(default_factory=list, description="List of step IDs that must complete before this step can start.")
    inputs: dict[str, str] = Field(default_factory=dict, description="Templated input values from previous steps (e.g., {'context': '{{steps.s1.output}}'}).")
    expected_output: str = Field(..., description="Brief description of what this step is expected to produce.")
    side_effect: bool = Field(False, description="True if this step modifies data (sends email, updates DB, creates event).")
    requires_confirmation: bool = Field(False, description="True if this step requires explicit user approval before execution.")

class ExecutionPlan(BaseModel):
    steps: list[Step] = Field(..., description="Ordered list of steps to execute the user's request.")
    rewritten_intent: str = Field(..., description="A refined summary of the user's request.")
    final_response_instruction: str = Field(..., description="Instruction on how to synthesize the final response.")
    error_policy: Literal["retry", "ask_user", "fail_fast", "skip"] = Field("ask_user", description="Strategy for handling step failures.")
    clarifying_questions: list[str] = Field(default_factory=list, description="Questions to ask the user if the request is ambiguous. If present, steps should be empty.")

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")
    database: Optional[str] = Field(None, description="The specific database to query. Defaults to Salesforce if not specified.")

class CitableResult(BaseModel):
    answer: str = Field(..., description="The factual answer based on the retrieved information.")
    sources: list[str] = Field(default_factory=list, description="List of source labels or citations used to form the answer.")

# -------- MCP Server Initializations --------

# MySQL
mysql_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
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

# Custom Email MCP
email_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["mcp_servers/email_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

# Custom Calendar MCP
calendar_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["mcp_servers/calendar_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)

# Custom Drive MCP
drive_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["mcp_servers/drive_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)


# RAG MCP (Pinecone)
rag_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["mcp_servers/rag_server.py"],
    env={
        "PINECONE_API_KEY": os.getenv("PINECONE_API_KEY", ""),
        "PINECONE_INDEX_NAME": os.getenv("PINECONE_INDEX_NAME", "drive-rag"),
        "PINECONE_MODEL": os.getenv("PINECONE_MODEL", "all-MiniLM-L6-v2"),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
    },
    timeout=120,
)

# Custom Jira MCP
jira_mcp = MCPServerStdio(
    "/Users/chetan/Documents/GitHub/vertical_aiAgent/.venv/bin/python3",
    args=["mcp_servers/jira_server.py"],
    env={
        "JIRA_API_KEY": os.getenv("JIRA_API_KEY", ""),
        "JIRA_API_SECRET": os.getenv("JIRA_API_SECRET", ""),
    }
)


manager_agent = Agent(
    "google-gla:gemini-3-flash-preview",
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
        "- 'email': Sending and reading emails (Gmail). to send emails \n"
        "- 'sql': Querying the business database (Salesforce data).\n"
        "- 'drive': File management (Drive) and retrieving knowledge from stored documents (PDFs, reports, etc).\n"
        "- 'jira': Project management, issue tracking, and software development tasks.\n"
        "   - Capabilities: Create/Update issues, Add comments, Transition status, Search issues.\n"
        "- 'general': Simple conversational, or reply with 'I can't help with that' whenever the question is not related to the above agents.\n\n"
        "RULES:\n"
        "1. ANALYZE context: You have access to the full conversation history. Resolve references like 'that meeting' or 'the file' based on previous turns.\n"
        "2. CHAIN steps: If a task requires output from one agent to be used by another (e.g., 'email the meeting summary'), create sequential steps.\n"
        "   - Step s1: tldv ('Summarize the meeting...')\n"
        "   - Step s2: email ('Send an email...')\n"
        "     - depends_on: ['s1']\n"
        "     - inputs: {'body': '{{steps.s1.output}}'}\n"
        "3. INPUT TEMPLATING:\n"
        "   - Use `{{steps.STEP_ID.output}}` to reference results from previous steps.\n"
        "   - Example: Instruction 'Send email to {{steps.s1.output}} with content {{steps.s2.output}}'\n"
        "4. SIDE EFFECTS & CONFIRMATION:\n"
        "   - Set `side_effect=True` for ANY action that modifies external state (sending emails, creating events, deleting files, updating DB). This is very important for safety.\n"
        "   - Set `requires_confirmation=True` for high-risk side effects (e.g., sending emails, creating events, updating DB, deleting data) or if details are ambiguous.This is very important for safety.\n"
        "5. CLARIFYING QUESTIONS:\n"
        "   - If the user's request is too vague to form a plan (e.g., 'Send an email' without recipient or subject), DO NOT create steps.\n"
        "   - Instead, populate `clarifying_questions` with 1-2 specific questions to ask the user.\n"
        "6. TLDV vs CALENDAR: \n"
        "   - If the user asks about CONTENT (what was said, summaries, topics) -> TLDV.\n"
        "   - If the user asks about LOGISTICS (when is it, invite who) -> CALENDAR.\n"
    )
)

email_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Gmail automation assistant with access to Google Workspace tools.\n\n"
        "Capabilities:\n"
        "- Search and retrieve emails with filters (sender, subject, date range)\n"
        "- Send emails with rich formatting and attachments\n"
        "- Read email content and extract information\n\n"
        "Guidelines:\n"
        "- When instructed to send an email, execute the send_email tool with the provided details.\n"
        "- The Manager Agent handles confirmation requirements, so execute instructions as given.\n"
        "- Provide clear summaries of email content.\n"
        "- CRITICAL: If asked to read/reply, FIRST search for the email ID."
    ),
    mcp_servers=[email_mcp]
)

drive_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    output_type=CitableResult,
    system_prompt=(
        "You are a Google Drive file management and knowledge assistant.\n\n"
        "Capabilities:\n"
        "- Search and retrieve knowledge from stored documents (PDFs, Docs, Text) using 'search_documents'\n"
        "- Search files/folders metadata (listing) using natural language or Drive query syntax via 'search_files'\n"
        "- Read content of specific files if you have the ID\n"
        "- Upload, download, create folders\n"
        "- Get file metadata\n\n"
        "Guidelines:\n"
        "- **Content vs. Files**:\n"
        "  - If the user asks about *information inside* documents (e.g., 'What does the budget report say?', 'Find info about project X'), use `search_documents`. This uses semantic search over the knowledge base.\n"
        "  - If the user asks for *files themselves* (e.g., 'List my PDF files', 'Find the file named report.pdf'), use `search_files` (Drive API).\n"
        "- **RAG Search (`search_documents`)**:\n"
        "  - Use this for open-ended questions about knowledge.\n"
        "  - Results include 'citation_label' and snippets.\n"
        "- **Citations**:\n"
        "  - Populate the `sources` field in your output with the `citation_label` of every document used.\n"
        "  - Do not include citations in the `answer` text; keep them in the `sources` list.\n"
        "- **Drive Management**:\n"
        "  - Provide clear file listings with names, types, and sizes.\n"
        "  - Report upload/download progress.\n"
        "- CRITICAL: Do not confuse `search_documents` (content/knowledge) with `search_files` (file system/metadata).\n"
    ),
    mcp_servers=[rag_mcp, drive_mcp]
)

calendar_agent = Agent(
    "google-gla:gemini-3-flash-preview",
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

tldv_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    output_type=CitableResult,
    system_prompt=(
        "You are a TLDV Meeting Notetaker assistant with advanced search capabilities.\n\n"
        "## Core Capabilities\n"
        "- Search across all past meeting transcripts using semantic vector search\n"
        "- Filter by date ranges, speakers, and specific meetings\n"
        "- Retrieve the most relevant context with high accuracy\n\n"
        "## Search Tool Usage\n"
        "You have access to 'search_meetings' with advanced parameters:\n\n"
        "**Key Parameters:**\n"
        "- `query` (required): Natural language question or topic\n"
        "- `min_similarity` (default: 0.3): Relevance threshold. Use 0.3 for broad searches.\n\n"
        "**Filtering Parameters:**\n"
        "- `start_date` / `end_date`: ISO format or natural language (e.g. '2024-12-01')\n"
        "- `speaker`: Partial match on participants\n"
        "- `meeting_id`: Specific meeting scope\n\n"
        "**Quality Parameters:**\n"
        "- `deduplicate` (default: True): Prevents info overload by limiting chunks per meeting\n"
        "- `max_results_per_meeting` (default: 3): Max chunks from same meeting\n\n"
        "## Best Practices\n"
        "1. **Use filters proactively**: If user mentions time ('last month'), dates, or speakers, USE the filters.\n"
        "2. **Interpret results carefully**:\n"
        "   - **Score 0.3 - 0.45**: Potential match. Review content carefully.\n"
        "   - **Score > 0.45**: High confidence match.\n"
        "   - Use `citation_label` for the `sources` field.\n"
        "   - Use `metadata` to add context (date, speakers, meeting title)\n"
        "5. **Citation**: Populate the `sources` field in your output with the `citation_label` of every meeting used.\n"
        "6. **Multi-step searches**: If first search is too narrow, try broader query or lower threshold.\n\n"
        "## Response Format\n"
        "When presenting results:\n"
        "- Put the synthesis in the `answer` field.\n"
        "- Put all unique meeting citations in the `sources` field.\n"
        "- Do not include citations in the `answer` text itself; the system handles those separately.\n\n"
        "## Important Distinctions\n"
        "- You handle PAST meeting content (transcripts, what was said)\n"
        "- For FUTURE meetings (scheduling, invites) → defer to Calendar agent\n"
    ),
    mcp_servers=[rag_mcp]
)

jira_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Jira project management assistant.\n\n"
        "Capabilities:\n"
        "- Search for issues using JQL or natural language\n"
        "- Create new issues (Tasks, Bugs, Stories) in specific projects\n"
        "- Update existing issues (status, assignee, priority)\n"
        "- Add comments to issues\n"
        "- Get detailed issue information\n\n"
        "Guidelines:\n"
        "- When creating issues, ask for project key if not provided (default to 'PROJ' if necessary but prefer asking)\n"
        "- Use clear summaries and descriptions\n"
        "- When searching, provide key details: Key, Summary, Status, Assignee, Priority\n"
        "- CRITICAL: If asked to update an issue, FIRST search for it to confirm the Key.\n"
    ),
    mcp_servers=[jira_mcp]
)

sql_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    output_type=sql,
    deps_type=SqlDeps,
    mcp_servers=[mysql_mcp]
)

@sql_agent.system_prompt
def sql_system_prompt(ctx: RunContext[SqlDeps]) -> str:
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
    system_prompt=(
        "You are a helpful, knowledgeable AI assistant.\n\n"
        "When answering questions:\n"
        "- Provide accurate, concise information\n"
        "- Use examples when helpful\n"
        "- Admit when you don't know something\n"
        "- Be conversational and friendly\n\n"
        "## Synthesis Guidelines\n"
        "When you are synthesizing results from other agents:\n"
        "1. Focus on the factual content provided in the `Execution Results`.\n"
        "2. **SOURCES & CITATIONS**: You will be provided with a list of 'Verified Sources'.\n"
        "   - You MUST include a 'Sources & Citations' section at the end of your response.\n"
        "   - List all verified sources provided to you in a clean markdown list.\n"
        "   - Do not hallucinate sources; only use the ones explicitly provided.\n\n"
        "Note: You don't have access to external tools for general queries. "
        "Inform users if they need specific functionality like email or database access."
    )
)

@observe()
async def initialize_agents() -> SqlDeps:
    """Initialize agents and return SQL dependencies for dependency injection."""
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
        # Fetch ALL schemas (utils.py filters the system ones)
        schema_info_result = await mysql_mcp.direct_call_tool(
            name="schema_info", 
            args={} 
        )
        if schema_info_result.get("success"):
            # Only include Salesforce database and use lightweight format to save tokens
            formatted_schema = format_schema_rows(
                schema_info_result["schema"], 
                include_dbs=["Salesforce", "information_schema"], 
                lightweight=True
            )
            logger.info(f"📊 Schema loaded successfully ({len(schema_info_result['schema'])} columns)")
    except Exception as e:
        logger.error(f"❌ Error fetching schema: {e}", exc_info=True)
    
    # Create SQL dependencies object for dependency injection
    sql_deps = SqlDeps(
        schema_text=formatted_schema,
        business_context=custom_sql_context,
        approved_databases=["Salesforce", "mysql", "information_schema"],
        default_database="Salesforce"
    )
    
    init_duration = time.time() - init_start
    logger.info(f"✅ Agents Initialization Complete ({init_duration:.2f}s)")
    
    return sql_deps
