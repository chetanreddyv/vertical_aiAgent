from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from enum import Enum
from typing import Optional
import os
import asyncio
from dotenv import load_dotenv
import logging


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("Starting initialization...")
load_dotenv()

# Load SQL context from file
sql_context_file = "sql_context.md"
custom_sql_context = ""
try:
    if os.path.exists(sql_context_file):
        with open(sql_context_file, "r") as f:
            custom_sql_context = f.read()
        logger.info(f"Loaded SQL context from {sql_context_file}")
    else:
        logger.warning(f"SQL context file {sql_context_file} not found")
except Exception as e:
    logger.error(f"Failed to read {sql_context_file}: {e}")

# Fallback to env var if file content is empty
if not custom_sql_context:
    custom_sql_context = os.getenv("CUSTOM_SQL_CONTEXT", "")

# Required environment variables including Google OAuth
required_vars = [
    "EMAIL_PASSWORD", "EMAIL_ADDRESS", "DB_HOST", "DB_USER", 
    "DB_PASSWORD", "OPENAI_API_KEY", 
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"
]
for var in required_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        raise ValueError(f"{var} not found in .env file")

# Intent handling extended for Google Workspace services
class IntentType(str, Enum):
    EMAIL_ONLY = "email_only"
    SQL_ONLY = "sql_only"
    EMAIL_AND_SQL = "email_and_sql"
    DRIVE_ONLY = "drive_only"
    CALENDAR_ONLY = "calendar_only"
    DOCS_ONLY = "docs_only"
    MULTI_WORKSPACE = "multi_workspace"
    GENERAL = "general"

class ExecutionPlan(BaseModel):
    intent: IntentType
    email_task: Optional[str]
    sql_task: Optional[str]
    drive_task: Optional[str] = None
    calendar_task: Optional[str] = None
    docs_task: Optional[str] = None

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")

# -------- MCP Server Initializations --------

# MySQL (unchanged)
mysql_mcp = MCPServerStdio(
    "python",
    args=["sql_server.py"],
    env={
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_PORT": os.getenv("DB_PORT", "3306"),
        "DB_USER": os.getenv("DB_USER"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "CUSTOM_SQL_CONTEXT": custom_sql_context,
    },
    timeout=60,
)


# Custom Calendar MCP
calendar_mcp = MCPServerStdio(
    "python",
    args=["calendar_server.py"],
    env={
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    }
)


# -------- Agent Definitions (all use workspace MCP except SQL) -------

intent_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=ExecutionPlan,
    system_prompt=(
        "You are an intent classification expert. Analyze the user's request and determine the primary action they want to perform.\n\n"
        "Classification Options:\n"
        "- 'email_only': Gmail operations (send, read, search emails)\n"
        "- 'sql_only': Database queries or data retrieval from MySQL\n"
        "- 'email_and_sql': Combined workflow (query database, then email results)\n"
        "- 'drive_only': Google Drive operations (upload, download, search files)\n"
        "- 'calendar_only': Google Calendar operations (create events, check schedule)\n"
        "- 'docs_only': Google Docs operations (create, edit documents)\n"
        "- 'multi_workspace': Multiple Google Workspace services needed\n"
        "- 'general': General questions or conversations not requiring tools\n\n"
        "For each classification, extract and rephrase the specific task in clear, actionable language."
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
        "- Format responses in a clear, readable manner"
    )
)

drive_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a Google Drive file management assistant.\n\n"
        "Capabilities:\n"
        "- Search files and folders by name, type, or content\n"
        "- Upload and download files\n"
        "- Create and organize folders\n"
        "- Share files and manage permissions\n"
        "- Get file metadata and details\n\n"
        "Guidelines:\n"
        "- Provide clear file listings with names, types, and sizes\n"
        "- Confirm before deleting or modifying files\n"
        "- Use descriptive folder structures\n"
        "- Report upload/download progress and success"
    )
)

calendar_agent = Agent(
    "openai:gpt-4o",
    system_prompt=(
        "You are a Google Calendar scheduling assistant.\n\n"
        "Capabilities:\n"
        "- Create calendar events with dates, times, and descriptions\n"
        "- Search for existing events\n"
        "- Update or cancel events\n"
        "- Check availability and find free time slots\n"
        "- Set reminders and notifications\n\n"
        "Guidelines:\n"
        "- Parse natural language dates and times accurately\n"
        "- Default to 1-hour duration if not specified\n"
        "- Use the user's local timezone\n"
        "- Confirm event details before creation\n"
        "- Provide clear summaries of scheduled events"
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
        "- Provide summaries of document content"
    )
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

def format_schema_rows(schema_rows):
    schema_map = {}
    for row in schema_rows:
        db = row.get('TABLE_SCHEMA') or 'defaultdb'
        tbl = row.get('TABLE_NAME')
        col = row.get('COLUMN_NAME')
        typ = row.get('DATA_TYPE')
        nullable = row.get('IS_NULLABLE', 'UNKNOWN')
        if db not in schema_map:
            schema_map[db] = {}
        if tbl not in schema_map[db]:
            schema_map[db][tbl] = []
        schema_map[db][tbl].append({
            'name': col,
            'type': typ,
            'nullable': nullable
        })
    schema_text = "=" * 60 + "\nDATABASE SCHEMA OVERVIEW\n" + "=" * 60 + "\n\n"
    for db_name, tables in schema_map.items():
        schema_text += f"📊 Database: {db_name}\n" + "-"*60 + "\n"
        for table_name, columns in tables.items():
            schema_text += f"\n  Table: {table_name}\n  Columns ({len(columns)} total):\n"
            for col in columns:
                nullable_text = "✓" if col['nullable'] == 'YES' else "✗"
                schema_text += f"    • {col['name']} ({col['type']}) - Nullable: {nullable_text}\n"
            schema_text += "\n"
        schema_text += "\n"
    logger.info("Schema formatting completed successfully")
    return schema_text

def format_query_results(result_data):
    """Format SQL query results into a readable table."""
    if not result_data.get('success'):
        return f"❌ Query Error: {result_data.get('error', 'Unknown error')}"
    
    rows = result_data.get('rows', [])
    if not rows:
        return "✅ Query executed successfully. No rows returned."
    
    # Get column names from first row
    columns = list(rows[0].keys())
    
    # Calculate column widths
    col_widths = {}
    for col in columns:
        col_widths[col] = max(
            len(str(col)),
            max(len(str(row.get(col, ''))) for row in rows)
        )
    
    # Build table header
    header = " | ".join(str(col).ljust(col_widths[col]) for col in columns)
    separator = "-+-".join("-" * col_widths[col] for col in columns)
    
    # Build table rows
    table_rows = []
    for row in rows:
        table_rows.append(" | ".join(
            str(row.get(col, '')).ljust(col_widths[col]) for col in columns
        ))
    
    # Combine everything
    output = f"\n{header}\n{separator}\n"
    output += "\n".join(table_rows)
    output += f"\n\n📊 Total rows: {len(rows)}"
    
    return output


async def main():
    logger.info("="*60)
    logger.info("Main loop starting...")
    logger.info("="*60)
    logger.info("Agents initialized:")
    logger.info("  - Intent Agent (classifier)")
    logger.info("  - Email Agent (Gmail via Workspace MCP)")
    logger.info("  - Drive Agent (Google Drive via Workspace MCP)")
    logger.info("  - Calendar Agent (Google Calendar via Workspace MCP)")
    logger.info("  - Docs Agent (Google Docs via Workspace MCP)")
    logger.info("  - SQL Agent (MySQL MCP)")
    logger.info("  - General Agent (fallback)")
    logger.info("="*60)

    # Run all relevant MCP servers in parallel
    # Run all relevant MCP servers in parallel
    async with email_agent.run_mcp_servers(), sql_agent.run_mcp_servers(), calendar_agent.run_mcp_servers():
        print("="*60)
        print("🚀 AGENTIC ORCHESTRATION WITH MYSQL & GOOGLE WORKSPACE MCP")
        print("="*60)
        print("Type your request, hit enter:")
        print("  - 'Check my latest 5 emails'\n  - 'Show tables in Salesforce database'")
        print("  - 'Create a calendar event tomorrow 2pm'\n  - 'clear' to clear screen, 'quit' to exit.\n")

        # --- Initialize Message Histories ---
        # We need to maintain separate histories for each agent to support multi-turn conversations
        agent_histories = {
            "email": [],
            "sql": [],
            "drive": [],
            "calendar": [],
            "docs": [],
            "general": [],
            "intent": [] 
        }

        # --- Load schema for SQL agent ---
        logger.info("Fetching database schema info...")
        try:
            schema_info_result = await mysql_mcp.direct_call_tool(name="schema_info", args={"database": "Salesforce"})
            if schema_info_result.get("success"):
                formatted_schema = format_schema_rows(schema_info_result["schema"])
            else:
                formatted_schema = "Schema unavailable."
        except Exception as e:
            logger.error(f"Error fetching schema: {e}", exc_info=True)
            formatted_schema = "Schema unavailable."

        # Update SQL agent's system prompt
        sql_agent.system_prompt = f"""You control the Salesforce MySQL database. Guidelines:
1. Use SELECT unless asked to modify data.
2. Proper SQL syntax only.
3. JOIN as needed, add WHERE and LIMIT clauses.
4. Return meaningful column names.
DB SCHEMA:
{formatted_schema}
CONTEXT:
{custom_sql_context}
Provide a valid query and brief explanation."""

        # --- Main interaction loop ---
        while True:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break
            if user_input.lower() == "clear":
                os.system("clear")
                continue

            logger.info(f"📥 User input received: '{user_input[:50]}...' ({len(user_input)} chars)")
            logger.info("🔍 Classifying intent using Intent Agent...")
            try:
                # Intent agent doesn't really need history as it classifies the current input
                # But we could pass it if we wanted context-aware classification
                intent_result = await intent_agent.run(user_input, message_history=agent_histories["intent"])
                agent_histories["intent"] = intent_result.new_messages()
                plan = intent_result.output
                logger.info(f"✅ Intent classified as: {plan.intent.value}")
                if plan.email_task:
                    logger.info(f"   - Email task: {plan.email_task[:50]}...")
                if plan.sql_task:
                    logger.info(f"   - SQL task: {plan.sql_task[:50]}...")
                if plan.drive_task:
                    logger.info(f"   - Drive task: {plan.drive_task[:50]}...")
                if plan.calendar_task:
                    logger.info(f"   - Calendar task: {plan.calendar_task[:50]}...")
                if plan.docs_task:
                    logger.info(f"   - Docs task: {plan.docs_task[:50]}...")
            except Exception as e:
                logger.error(f"❌ Intent classification failed: {e}", exc_info=True)
                print(f"\n❌ Error during intent classification: {e}\n{'-'*60}")
                continue

            logger.info(f"🚀 Executing agent workflow for intent: {plan.intent.value}")
            try:
                if plan.intent == IntentType.EMAIL_ONLY and plan.email_task:
                    logger.info("📧 Calling Email Agent (Gmail)")
                    agent_result = await email_agent.run(plan.email_task, message_history=agent_histories["email"])
                    agent_histories["email"] = agent_result.new_messages()
                    logger.info("✅ Email Agent execution completed")
                elif plan.intent == IntentType.SQL_ONLY and plan.sql_task:
                    logger.info("🗄️  Calling SQL Agent (MySQL)")
                    sql_result = await sql_agent.run(plan.sql_task, message_history=agent_histories["sql"])
                    agent_histories["sql"] = sql_result.new_messages()
                    logger.info("✅ SQL Agent execution completed")
                    
                    # Execute the generated query
                    logger.info("🔍 Executing generated SQL query...")
                    query = sql_result.output.sqlquery
                    explanation = sql_result.output.explanation or "No explanation provided"
                    
                    query_result = await mysql_mcp.direct_call_tool(
                        name="execute_query",
                        args={"query": query, "database": "Salesforce", "read_only": True}
                    )
                    
                    # Format results
                    formatted_results = format_query_results(query_result)
                    
                    # Combine explanation and results
                    agent_result = type('obj', (object,), {
                        'output': f"📝 Explanation: {explanation}\n\n🔍 Query: {query}\n{formatted_results}"
                    })()
                    logger.info("✅ Query executed and formatted")
                elif plan.intent == IntentType.DRIVE_ONLY and plan.drive_task:
                    logger.info("📁 Calling Drive Agent (Google Drive)")
                    agent_result = await drive_agent.run(plan.drive_task, message_history=agent_histories["drive"])
                    agent_histories["drive"] = agent_result.new_messages()
                    logger.info("✅ Drive Agent execution completed")
                elif plan.intent == IntentType.CALENDAR_ONLY and plan.calendar_task:
                    logger.info("📅 Calling Calendar Agent (Google Calendar)")
                    agent_result = await calendar_agent.run(plan.calendar_task, message_history=agent_histories["calendar"])
                    agent_histories["calendar"] = agent_result.new_messages()
                    logger.info("✅ Calendar Agent execution completed")
                elif plan.intent == IntentType.DOCS_ONLY and plan.docs_task:
                    logger.info("📝 Calling Docs Agent (Google Docs)")
                    agent_result = await docs_agent.run(plan.docs_task, message_history=agent_histories["docs"])
                    agent_histories["docs"] = agent_result.new_messages()
                    logger.info("✅ Docs Agent execution completed")
                elif plan.intent == IntentType.EMAIL_AND_SQL and plan.sql_task and plan.email_task:
                    logger.info("🔄 Multi-agent workflow: SQL → Email")
                    logger.info("  Step 1/2: Calling SQL Agent")
                    sql_result = await sql_agent.run(plan.sql_task, message_history=agent_histories["sql"])
                    agent_histories["sql"] = sql_result.new_messages()
                    logger.info("  ✅ SQL Agent completed")
                    
                    # Execute the generated query
                    logger.info("  🔍 Executing generated SQL query...")
                    query = sql_result.output.sqlquery
                    query_result = await mysql_mcp.direct_call_tool(
                        name="execute_query",
                        args={"query": query, "database": "Salesforce", "read_only": True}
                    )
                    formatted_results = format_query_results(query_result)
                    
                    logger.info("  Step 2/2: Calling Email Agent with SQL results")
                    enriched_task = f"{plan.email_task}\n\nDatabase Query Results:\n{formatted_results}"
                    agent_result = await email_agent.run(enriched_task, message_history=agent_histories["email"])
                    agent_histories["email"] = agent_result.new_messages()
                    logger.info("  ✅ Email Agent completed")
                    logger.info("✅ Multi-agent workflow completed")
                else:
                    logger.info("💬 Calling General Agent (fallback)")
                    agent_result = await general_agent.run(user_input, message_history=agent_histories["general"])
                    agent_histories["general"] = agent_result.new_messages()
                    logger.info("✅ General Agent execution completed")

                logger.info("✅ Agent workflow completed successfully")
                print(f"\n🤖 Assistant:\n{agent_result.output}\n{'-'*60}")
            except Exception as e:
                logger.error(f"❌ Agent execution error: {e}", exc_info=True)
                print(f"\n❌ Error: {e}\n{'-'*60}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nApplication terminated by user.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nFatal error: {e}")
