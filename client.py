from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()

if not os.getenv("EMAIL_PASSWORD"):
    raise ValueError("EMAIL_PASSWORD not found in .env file")
if not os.getenv("EMAIL_ADDRESS"):
    raise ValueError("EMAIL_ADDRESS not found in .env file")

# MySQL MCP Server
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

gmail_server = MCPServerStdio(
    "docker",
    args=[
        "run",
        "--platform", "linux/amd64",
        "-i", "--rm",
        "-e", "EMAIL_ADDRESS",
        "-e", "IMAP_HOST",
        "-e", "IMAP_PORT",
        "-e", "SMTP_HOST",
        "-e", "SMTP_PORT",
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

agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt=(
        "You are a helpful assistant with access to Gmail and MySQL database. "
        "Use Gmail tools to read, search, and send emails when requested. "
        "Use MySQL tools to answer questions about the database. "
        "Always confirm actions before sending emails."
    ),
    instrument=True,
    mcp_servers=[gmail_server, mysql_mcp],
)



async def main():
    print("Starting Gmail & MySQL MCP Agent...")
    print("Docker will pull the gmail-mcp image if needed (one-time setup).\n")

    async with agent.run_mcp_servers():
        result = await agent.run("List all the tools you have access to (Gmail and MySQL) and confirm you're ready.")
        print(f"Agent: {result.output}\n")  # CHANGED from result.data

        print("Type your requests below. Examples:")
        print("  - 'Check my latest emails'")
        print("  - 'Search for emails from john@example.com'")
        print("  - 'Send an email to...'")
        print("  - 'List all database tables'")
        print("  - 'Query the database for...'")
        print("  - Type 'quit' to exit\n")

        while True:
            user_input = input("> ")
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break

            # Either keep only the new messages or the entire history:
            history = result.new_messages()  # or result.all_messages()
            result = await agent.run(user_input, message_history=history)
            print(f"\nAgent: {result.output}\n")  # CHANGED from result.data

if __name__ == "__main__":
    asyncio.run(main())
