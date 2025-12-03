from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from dotenv import load_dotenv
from contextlib import asynccontextmanager

# Import the client agents and utilities
import agents
from agents import (
    intent_agent, email_agent, sql_agent, drive_agent, calendar_agent, 
    docs_agent, general_agent, mysql_mcp, calendar_mcp, drive_mcp,
    IntentType
)
from utils import format_query_results, get_temporal_context

import os

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state for agent histories and schema
agent_histories = {
    "email": [],
    "sql": [],
    "drive": [],
    "calendar": [],
    "docs": [],
    "general": [],
    "intent": []
}
formatted_schema = ""

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize agents and MCP servers on startup"""
    global formatted_schema
    logger.info("Initializing MCP servers and fetching schema...")
    
    # Start MCP servers
    async with (
        email_agent.run_mcp_servers(),
        sql_agent.run_mcp_servers(),
        calendar_agent.run_mcp_servers(),
        drive_agent.run_mcp_servers()
    ):
        # Initialize agents (load schema, set prompts)
        try:
            formatted_schema = await agents.initialize_agents()
            logger.info("✅ Agents initialized successfully")
        except Exception as e:
            logger.error(f"Error during initialization: {e}", exc_info=True)
            formatted_schema = "Schema unavailable."
        
        yield
        
        logger.info("Shutting down MCP servers...")

app = FastAPI(
    title="AI Agent API",
    description="FastAPI backend for multi-agent orchestration with MySQL & Google Workspace",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response models
class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = "default"

class QueryResponse(BaseModel):
    intent: str
    response: str
    success: bool
    error: Optional[str] = None
    sql_data: Optional[Dict[str, Any]] = None  # Structured SQL results
    status_updates: Optional[list[str]] = None  # Processing status messages

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "AI Agent API",
        "agents": ["email", "sql", "drive", "calendar", "docs", "general"]
    }

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    """
    Process a user query through the agent orchestration system
    """
    try:
        user_input = request.query.strip()
        if not user_input:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # Collect status updates
        status_updates = []
        
        logger.info(f"📥 Received query: '{user_input[:50]}...'")
        
        # Classify intent
        status_updates.append("Analyzing your request...")
        logger.info("🔍 Classifying intent...")
        
        # Add temporal context
        temporal_context = get_temporal_context()
        
        intent_result = await intent_agent.run(f"{temporal_context}{user_input}", message_history=agent_histories["intent"])
        agent_histories["intent"] = intent_result.new_messages()
        plan = intent_result.output
        
        status_updates.append(f"Detected intent: {plan.intent.value.replace('_', ' ').title()}")
        
        logger.info(f"✅ Intent: {plan.intent.value}")
        
        # Execute appropriate agent workflow
        agent_result = None
        
        if plan.intent == IntentType.EMAIL_ONLY and plan.email_task:
            status_updates.append("Processing email request...")
            logger.info("📧 Executing Email Agent")
            agent_result = await email_agent.run(f"{temporal_context}{plan.email_task}", message_history=agent_histories["email"])
            agent_histories["email"] = agent_result.new_messages()
            status_updates.append("Email operation completed")
            
        elif plan.intent == IntentType.SQL_ONLY and plan.sql_task:
            status_updates.append("Generating SQL query...")
            logger.info("🗄️ Executing SQL Agent")
            sql_result = await sql_agent.run(f"{temporal_context}{plan.sql_task}", message_history=agent_histories["sql"])
            agent_histories["sql"] = sql_result.new_messages()
            
            # Get the generated query and explanation
            query = sql_result.output.sqlquery
            explanation = sql_result.output.explanation or "No explanation provided"
            
            # Show SQL query immediately (before execution)
            status_updates.append(f"📝 {explanation}")
            status_updates.append(f"🔍 Generated Query: {query}")
            status_updates.append("Executing database query...")
            query_result = await mysql_mcp.direct_call_tool(
                name="execute_query",
                args={"query": query, "database": "Salesforce", "read_only": True}
            )
            
            status_updates.append("Formatting query results...")
            formatted_results = format_query_results(query_result)
            
            # Create response object with structured data
            agent_result = type('obj', (object,), {
                'output': f"📝 Explanation: {explanation}\n\n🔍 Query: {query}\n{formatted_results}",
                'sql_data': query_result if query_result.get('success') else None
            })()
            status_updates.append("Query completed successfully")
            
        elif plan.intent == IntentType.DRIVE_ONLY and plan.drive_task:
            status_updates.append("Accessing Google Drive...")
            logger.info("📁 Executing Drive Agent")
            agent_result = await drive_agent.run(f"{temporal_context}{plan.drive_task}", message_history=agent_histories["drive"])
            agent_histories["drive"] = agent_result.new_messages()
            status_updates.append("Drive operation completed")
            
        elif plan.intent == IntentType.CALENDAR_ONLY and plan.calendar_task:
            status_updates.append("Accessing Google Calendar...")
            logger.info("📅 Executing Calendar Agent")
            agent_result = await calendar_agent.run(f"{temporal_context}{plan.calendar_task}", message_history=agent_histories["calendar"])
            agent_histories["calendar"] = agent_result.new_messages()
            status_updates.append("Calendar operation completed")
            
        elif plan.intent == IntentType.DOCS_ONLY and plan.docs_task:
            status_updates.append("Accessing Google Docs...")
            logger.info("📝 Executing Docs Agent")
            agent_result = await docs_agent.run(f"{temporal_context}{plan.docs_task}", message_history=agent_histories["docs"])
            agent_histories["docs"] = agent_result.new_messages()
            status_updates.append("Docs operation completed")
            
        elif plan.intent == IntentType.EMAIL_AND_SQL and plan.sql_task and plan.email_task:
            logger.info("🔄 Executing Multi-agent workflow: SQL → Email")
            
            # Step 1: SQL
            status_updates.append("Step 1: Generating SQL query...")
            sql_result = await sql_agent.run(f"{temporal_context}{plan.sql_task}", message_history=agent_histories["sql"])
            agent_histories["sql"] = sql_result.new_messages()
            
            query = sql_result.output.sqlquery
            explanation = sql_result.output.explanation or "No explanation provided"
            
            status_updates.append("Step 2: Executing database query...")
            query_result = await mysql_mcp.direct_call_tool(
                name="execute_query",
                args={"query": query, "database": "Salesforce", "read_only": True}
            )
            formatted_results = format_query_results(query_result)
            
            # Step 2: Email with SQL results
            status_updates.append("Step 3: Preparing email with results...")
            enriched_task = f"{plan.email_task}\n\nSQL Query: {query}\n\nExplanation: {explanation}\n\nDatabase Query Results:\n{formatted_results}"
            email_result = await email_agent.run(f"{temporal_context}{enriched_task}", message_history=agent_histories["email"])
            agent_histories["email"] = email_result.new_messages()
            
            # Create combined result with SQL data
            agent_result = type('obj', (object,), {
                'output': email_result.output,
                'sql_data': query_result if query_result.get('success') else None
            })()
            status_updates.append("Multi-agent workflow completed")
            
        else:
            status_updates.append("Processing your question...")
            logger.info("💬 Executing General Agent (fallback)")
            agent_result = await general_agent.run(f"{temporal_context}{user_input}", message_history=agent_histories["general"])
            agent_histories["general"] = agent_result.new_messages()
            status_updates.append("Response generated")
        
        logger.info("✅ Query processed successfully")
        
        # Check if agent_result has sql_data attribute
        sql_data = getattr(agent_result, 'sql_data', None)
        
        return QueryResponse(
            intent=plan.intent.value,
            response=str(agent_result.output),
            success=True,
            sql_data=sql_data,
            status_updates=status_updates
        )
        
    except Exception as e:
        logger.error(f"❌ Error processing query: {e}", exc_info=True)
        return QueryResponse(
            intent="error",
            response="",
            success=False,
            error=str(e)
        )

async def query_stream_generator(query: str):
    """Generate status updates during query processing"""
    try:
        user_input = query.strip()
        if not user_input:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Query cannot be empty'})}\n\n"
            return

        # Classify intent
        yield f"data: {json.dumps({'type': 'status', 'content': 'Analyzing your request...'})}\n\n"
        
        temporal_context = get_temporal_context()
        
        intent_result = await intent_agent.run(f"{temporal_context}{user_input}", message_history=agent_histories["intent"])
        agent_histories["intent"] = intent_result.new_messages()
        plan = intent_result.output
        
        intent_display = plan.intent.value.replace("_", " ").title()
        yield f"data: {json.dumps({'type': 'status', 'content': f'Detected intent: {intent_display}'})}\n\n"
        
        # Execute appropriate agent workflow
        agent_result = None
        sql_data = None
        
        if plan.intent == IntentType.EMAIL_ONLY and plan.email_task:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Processing email request...'})}\n\n"
            agent_result = await email_agent.run(f"{temporal_context}{plan.email_task}", message_history=agent_histories["email"])
            agent_histories["email"] = agent_result.new_messages()
            yield f"data: {json.dumps({'type': 'status', 'content': 'Email operation completed'})}\n\n"
            
        elif plan.intent == IntentType.SQL_ONLY and plan.sql_task:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Generating SQL query...'})}\n\n"
            sql_result = await sql_agent.run(f"{temporal_context}{plan.sql_task}", message_history=agent_histories["sql"])
            agent_histories["sql"] = sql_result.new_messages()
            
            # Get the generated query and explanation
            query = sql_result.output.sqlquery
            explanation = sql_result.output.explanation or "No explanation provided"
            
            # Show SQL query immediately (before execution) with special type
            yield f"data: {json.dumps({'type': 'sql_query', 'query': query, 'explanation': explanation})}\n\n"
            yield f"data: {json.dumps({'type': 'status', 'content': 'Executing database query...'})}\n\n"
            query_result = await mysql_mcp.direct_call_tool(
                name="execute_query",
                args={"query": query, "database": "Salesforce", "read_only": True}
            )
            
            yield f"data: {json.dumps({'type': 'status', 'content': 'Formatting query results...'})}\n\n"
            formatted_results = format_query_results(query_result)
            
            # Create response object with structured data
            agent_result = type('obj', (object,), {
                'output': f"📝 Explanation: {explanation}\n\n🔍 Query: {query}\n{formatted_results}",
                'sql_data': query_result if query_result.get('success') else None
            })()
            sql_data = query_result if query_result.get('success') else None
            yield f"data: {json.dumps({'type': 'status', 'content': 'Query completed successfully'})}\n\n"
            
        elif plan.intent == IntentType.DRIVE_ONLY and plan.drive_task:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Accessing Google Drive...'})}\n\n"
            agent_result = await drive_agent.run(f"{temporal_context}{plan.drive_task}", message_history=agent_histories["drive"])
            agent_histories["drive"] = agent_result.new_messages()
            yield f"data: {json.dumps({'type': 'status', 'content': 'Drive operation completed'})}\n\n"
            
        elif plan.intent == IntentType.CALENDAR_ONLY and plan.calendar_task:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Accessing Google Calendar...'})}\n\n"
            agent_result = await calendar_agent.run(f"{temporal_context}{plan.calendar_task}", message_history=agent_histories["calendar"])
            agent_histories["calendar"] = agent_result.new_messages()
            yield f"data: {json.dumps({'type': 'status', 'content': 'Calendar operation completed'})}\n\n"
            
        elif plan.intent == IntentType.DOCS_ONLY and plan.docs_task:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Accessing Google Docs...'})}\n\n"
            agent_result = await docs_agent.run(f"{temporal_context}{plan.docs_task}", message_history=agent_histories["docs"])
            agent_histories["docs"] = agent_result.new_messages()
            yield f"data: {json.dumps({'type': 'status', 'content': 'Docs operation completed'})}\n\n"
            
        elif plan.intent == IntentType.EMAIL_AND_SQL and plan.sql_task and plan.email_task:
            # Step 1: SQL
            yield f"data: {json.dumps({'type': 'status', 'content': 'Step 1: Generating SQL query...'})}\n\n"
            sql_result = await sql_agent.run(f"{temporal_context}{plan.sql_task}", message_history=agent_histories["sql"])
            agent_histories["sql"] = sql_result.new_messages()
            
            query = sql_result.output.sqlquery
            explanation = sql_result.output.explanation or "No explanation provided"
            
            yield f"data: {json.dumps({'type': 'status', 'content': 'Step 2: Executing database query...'})}\n\n"
            query_result = await mysql_mcp.direct_call_tool(
                name="execute_query",
                args={"query": query, "database": "Salesforce", "read_only": True}
            )
            formatted_results = format_query_results(query_result)
            
            # Step 2: Email with SQL results
            yield f"data: {json.dumps({'type': 'status', 'content': 'Step 3: Preparing email with results...'})}\n\n"
            enriched_task = f"{plan.email_task}\n\nSQL Query: {query}\n\nExplanation: {explanation}\n\nDatabase Query Results:\n{formatted_results}"
            email_result = await email_agent.run(f"{temporal_context}{enriched_task}", message_history=agent_histories["email"])
            agent_histories["email"] = email_result.new_messages()
            
            # Create combined result with SQL data
            agent_result = type('obj', (object,), {
                'output': email_result.output,
                'sql_data': query_result if query_result.get('success') else None
            })()
            sql_data = query_result if query_result.get('success') else None
            yield f"data: {json.dumps({'type': 'status', 'content': 'Multi-agent workflow completed'})}\n\n"
            
        else:
            yield f"data: {json.dumps({'type': 'status', 'content': 'Processing your question...'})}\n\n"
            agent_result = await general_agent.run(f"{temporal_context}{user_input}", message_history=agent_histories["general"])
            agent_histories["general"] = agent_result.new_messages()
            yield f"data: {json.dumps({'type': 'status', 'content': 'Response generated'})}\n\n"
            
        # Send final result
        response_data = {
            "intent": plan.intent.value,
            "response": str(agent_result.output),
            "success": True,
            "sql_data": sql_data
        }
        yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"

    except Exception as e:
        logger.error(f"❌ Error processing query stream: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

@app.get("/query-stream")
async def stream_query(query: str):
    return StreamingResponse(
        query_stream_generator(query),
        media_type="text/event-stream"
    )

@app.post("/reset-history")
async def reset_history(session_id: str = "default"):
    """Reset conversation history for a session"""
    global agent_histories
    agent_histories = {
        "email": [],
        "sql": [],
        "drive": [],
        "calendar": [],
        "docs": [],
        "general": [],
        "intent": []
    }
    logger.info(f"🔄 Reset conversation history for session: {session_id}")
    return {"status": "success", "message": "Conversation history reset"}

@app.get("/schema")
async def get_schema():
    """Get the current database schema"""
    return {
        "schema": formatted_schema,
        "status": "available" if formatted_schema != "Schema unavailable." else "unavailable"
    }

@app.get("/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "agents": {
            "intent": "ready",
            "email": "ready",
            "sql": "ready",
            "drive": "ready",
            "calendar": "ready",
            "docs": "ready",
            "general": "ready"
        },
        "mcp_servers": {
            "mysql": "connected",
            "calendar": "connected",
            "drive": "connected"
        },
        "schema_loaded": formatted_schema != "Schema unavailable."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
