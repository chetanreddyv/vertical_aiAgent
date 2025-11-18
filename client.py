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
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("Starting application initialization...")
load_dotenv()

required_vars = ["EMAIL_PASSWORD", "EMAIL_ADDRESS", "DB_HOST", "DB_USER", "DB_PASSWORD", "OPENAI_API_KEY"]
for var in required_vars:
    if not os.getenv(var):
        logger.error(f"Missing required environment variable: {var}")
        raise ValueError(f"{var} not found in .env file")

class IntentType(str, Enum):
    EMAIL_ONLY = "email_only"
    SQL_ONLY = "sql_only"
    EMAIL_AND_SQL = "email_and_sql"
    GENERAL = "general"

class ExecutionPlan(BaseModel):
    intent: IntentType
    email_task: Optional[str]
    sql_task: Optional[str]

class sql(BaseModel):
    sqlquery: str = Field(..., description="The SQL query to execute.")
    explanation: Optional[str] = Field(None, description="A brief explanation of what the query does.")

logger.info("Initializing MySQL MCP server...")
mysql_mcp = MCPServerStdio(
    "python",
    args=["server.py"],
    env={
        "DB_HOST": os.getenv("DB_HOST", "localhost"),
        "DB_PORT": os.getenv("DB_PORT", "3306"),
        "DB_USER": os.getenv("DB_USER", ""),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "CUSTOM_SQL_CONTEXT": os.getenv("CUSTOM_SQL_CONTEXT", ""),
    },
    timeout=60,
)

logger.info("Initializing Gmail MCP server...")
gmail_server = MCPServerStdio(
    "docker",
    args=[
        "run", "--platform", "linux/amd64",
        "-i", "--rm",
        "-e", "EMAIL_ADDRESS",
        "-e", "IMAP_HOST", "-e", "IMAP_PORT",
        "-e", "SMTP_HOST", "-e", "SMTP_PORT",
        "-e", "EMAIL_PASSWORD",
        "yashtekwani/gmail-mcp",
    ],
    env={
        "EMAIL_ADDRESS": os.getenv("EMAIL_ADDRESS", ""),
        "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD", ""),
        "IMAP_HOST": os.getenv("IMAP_HOST", "imap.gmail.com"),
        "IMAP_PORT": os.getenv("IMAP_PORT", "993"),
        "SMTP_HOST": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "SMTP_PORT": os.getenv("SMTP_PORT", "587"),
    },
    timeout=30,
)

logger.info("Initializing Intent Agent...")
intent_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=ExecutionPlan,
    system_prompt=(
        "Classify the user's intent as 'email_only', 'sql_only', 'email_and_sql', or 'general'. "
        "Fill in the appropriate sub-task prompts for each downstream agent."
    ),
)

logger.info("Initializing Email Agent...")
email_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt="You control Gmail. Given any email-related request, respond with actions or results.",
    mcp_servers=[gmail_server]
)

# We will override system_prompt for SQL agent at runtime!
logger.info("Initializing SQL Agent...")
sql_agent = Agent(
    "openai:gpt-4o-mini",
    output_type=sql,
    system_prompt="PLACEHOLDER: This prompt will be updated at startup.",  # Updated after schema loaded
    mcp_servers=[mysql_mcp]
)

logger.info("Initializing General Agent...")
general_agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt="You are a helpful AI assistant for non-email/non-database queries."
)

def format_schema_rows(schema_rows):
    """
    Format schema information for the AI prompt in a structured, readable way.
    This helps the LLM better understand the database structure.
    """
    schema_map = {}
    
    # Organize schema data by database and table
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
    
    # Format into readable text
    schema_text = "=" * 60 + "\n"
    schema_text += "DATABASE SCHEMA OVERVIEW\n"
    schema_text += "=" * 60 + "\n\n"
    
    for db_name, tables in schema_map.items():
        schema_text += f"📊 Database: {db_name}\n"
        schema_text += "-" * 60 + "\n"
        
        for table_name, columns in tables.items():
            schema_text += f"\n  Table: {table_name}\n"
            schema_text += f"  Columns ({len(columns)} total):\n"
            
            for col in columns:
                nullable_text = "✓" if col['nullable'] == 'YES' else "✗"
                schema_text += f"    • {col['name']} ({col['type']}) - Nullable: {nullable_text}\n"
            
            schema_text += "\n"
        
        schema_text += "\n"
    
    logger.info("Schema formatting completed successfully")
    return schema_text


async def main():
    logger.info("="*60)
    logger.info("Starting main application loop...")
    logger.info("="*60)
    
    async with email_agent.run_mcp_servers(), sql_agent.run_mcp_servers():
        logger.info("MCP servers started successfully")
        print("="*60)
        print("🚀 SIMPLE MULTI-AGENT ORCHESTRATION SYSTEM [Persistent MCP + Schema]")
        print("="*60)
        print("Type your request and hit enter. Examples:")
        print("  - 'Check my latest 5 emails'")
        print("  - 'Show tables in Salesforce database'")
        print("  - 'clear' to clear screen, 'quit' to exit.\n")

        # ----- MCP Tool call DIRECTLY for schema_info -----
        logger.info("Fetching database schema information...")
        try:
            schema_info_result = await mysql_mcp.direct_call_tool(name="schema_info",args={"database": "Salesforce"})
            
            if schema_info_result.get("success"):
                logger.info("Schema retrieved successfully")
                formatted_schema = format_schema_rows(schema_info_result["schema"])
            else:
                logger.warning("Schema retrieval failed, using default message")
                formatted_schema = "Schema unavailable."
        except Exception as e:
            logger.error(f"Error fetching schema: {e}", exc_info=True)
            formatted_schema = "Schema unavailable."

        # Inject schema context into the SQL agent
        logger.info("Updating SQL agent system prompt with schema information...")
        sql_agent.system_prompt = f"""You control the Salesforce MySQL database. Given any SQL-related request, respond with actions or results.
IMPORTANT GUIDELINES:
1. Generate only SELECT queries unless explicitly asked to modify data.
2. Always use proper SQL syntax.
3. Use appropriate JOINs when multiple tables are involved.
4. Add WHERE clauses for filtering when relevant.
5. Use LIMIT clauses to prevent overwhelming results (default LIMIT 100 unless specified).
6. Return column names that are meaningful.
7. You have access to the Salesforce database schema below.

SALESFORCE DATABASE SCHEMA:
{formatted_schema}

Your response should include:
- A valid SQL query
- A brief explanation of what the query does.
"""

        # Agent orchestration as before...
        logger.info("Entering main interaction loop...")
        while True:
            user_input = input("You: ").strip()
            
            if not user_input:
                continue
                
            logger.info(f"User input received: '{user_input}'")
            
            if user_input.lower() in ["quit", "exit", "q"]:
                logger.info("User requested exit")
                print("Goodbye!")
                break
                
            if user_input.lower() == "clear":
                os.system("clear")
                continue

            logger.info("Step 1: Classifying user intent...")
            try:
                intent_result = await intent_agent.run(user_input)
                plan = intent_result.output
                logger.info(f"Intent classified as: {plan.intent.value}")
            except Exception as e:
                logger.error(f"Error during intent classification: {e}", exc_info=True)
                print(f"\n❌ Error during intent classification: {e}\n{'-'*60}")
                continue
                
            try:
                if plan.intent == IntentType.EMAIL_ONLY and plan.email_task:
                    logger.info("Step 2: Executing EMAIL_ONLY task...")
                    agent_result = await email_agent.run(plan.email_task)
                    logger.info("Email task completed successfully")
                    
                elif plan.intent == IntentType.SQL_ONLY and plan.sql_task:
                    logger.info("Step 2: Executing SQL_ONLY task...")
                    agent_result = await sql_agent.run(plan.sql_task)
                    logger.info("SQL task completed successfully")
                    
                elif plan.intent == IntentType.EMAIL_AND_SQL and plan.sql_task and plan.email_task:
                    logger.info("Step 2: Executing EMAIL_AND_SQL task (SQL first)...")
                    sql_result = await sql_agent.run(plan.sql_task)
                    logger.info("SQL task completed, now executing email task...")
                    
                    enriched_task = f"{plan.email_task}\n\nResults from database:\n{sql_result.output}"
                    agent_result = await email_agent.run(enriched_task)
                    logger.info("Email task completed successfully")
                    
                else:
                    logger.info("Step 2: Executing GENERAL task...")
                    agent_result = await general_agent.run(user_input)
                    logger.info("General task completed successfully")
                    
                logger.info("Step 3: Displaying results to user")
                print(f"\n🤖 Assistant:\n{agent_result.output}\n{'-'*60}")
                
            except Exception as e:
                logger.error(f"Error during task execution: {e}", exc_info=True)
                print(f"\n❌ Error: {e}\n{'-'*60}")

if __name__ == "__main__":
    logger.info("Application starting...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user (Ctrl+C)")
        print("\n\nApplication terminated by user.")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        print(f"\n\nFatal error: {e}")
    finally:
        logger.info("Application shutdown complete")
