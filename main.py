from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
import time
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import logging
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ModelRequest, UserPromptPart
from pydantic_ai import UsageLimits

# Import the client agents and utilities
import agents

from agents import (
    manager_agent, email_agent, sql_agent, drive_agent, calendar_agent, 
    tldv_agent, general_agent, mysql_mcp, calendar_mcp, 
    drive_mcp, rag_mcp, AgentSelection
)
from utils import format_query_results, get_temporal_context
from executor import PlanExecutor

import os

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global dependencies
conversation_history = []
sql_deps: Optional['SqlDeps'] = None  # Will be initialized on startup
plan_executor: Optional[PlanExecutor] = None

# Limits for specialist agents to prevent runaway executions
# manager_agent remains unlimited (default)
SPECIALIST_LIMITS = UsageLimits(request_limit=10, tool_calls_limit=5)


def keep_last_n_turns(message_history: list, n_turns: int = 15) -> list:
    """Keep only the last N conversation turns (user + assistant pairs)."""
    max_messages = n_turns * 2
    if len(message_history) > max_messages:
        return message_history[-max_messages:]
    return message_history

def get_agent_by_name(name: str):
    """Retrieve agent instance by enum name"""
    agents_map = {
        "email": email_agent,
        "sql": sql_agent,
        "drive": drive_agent,
        "calendar": calendar_agent,
        "tldv": tldv_agent,
        "general": general_agent
    }
    return agents_map.get(name, general_agent)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize agents and MCP servers on startup"""
    global sql_deps, plan_executor
    logger.info("Initializing MCP servers and fetching schema...")
    
    # Start MCP servers
    async with (
        email_agent.run_mcp_servers(),
        sql_agent.run_mcp_servers(),
        calendar_agent.run_mcp_servers(),
        drive_agent.run_mcp_servers(),
        tldv_agent.run_mcp_servers()
    ):
        # Initialize agents (load schema, create dependencies)
        try:
            sql_deps = await agents.initialize_agents()
            logger.info("✅ Agents initialized successfully")
        except Exception as e:
            logger.error(f"Error during initialization: {e}", exc_info=True)
            # Create minimal SqlDeps as fallback
            from agents import SqlDeps
            sql_deps = SqlDeps(
                schema_text="Schema unavailable.",
                business_context="",
                approved_databases=["Salesforce"],
                default_database="Salesforce"
            )
        
        # Initialize Executor with agents map
        agents_map = {
            "email": email_agent,
            "sql": sql_agent,
            "drive": drive_agent,
            "calendar": calendar_agent,
            "tldv": tldv_agent,
            "general": general_agent
        }
        plan_executor = PlanExecutor(agents_map)
        logger.info("✅ PlanExecutor initialized")

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
    clarification_needed: Optional[bool] = False
    clarifying_questions: Optional[list[str]] = None

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "AI Agent API",
        "agents": ["email", "sql", "drive", "calendar", "tldv", "general"]
    }

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    """
    Process a user query through the Manager Agent orchestration system
    """
    global conversation_history
    start_time = time.time()
    try:
        user_input = request.query.strip()
        if not user_input:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        status_updates = []
        logger.info(f"--- 📥 START PROCESSING QUERY ---")
        logger.info(f"Query: '{user_input}'")
        
        status_updates.append("Analyzing your request...")
        
        # 1. Manager Agent Plan
        logger.info("🔍 Manager Agent: Starting Planning...")
        manager_start = time.time()
        
        temporal_context = get_temporal_context()
        from agents import manager_agent
        plan_result = await manager_agent.run(
            f"{temporal_context}{user_input}", 
            message_history=conversation_history
        )
        plan = plan_result.output
        manager_duration = time.time() - manager_start
        logger.info(f"✅ Manager Agent: Planning Complete ({manager_duration:.2f}s)")
        
        # Check for clarifying questions
        if plan.clarifying_questions:
            logger.info(f"❓ Clarification Needed: {plan.clarifying_questions}")
            return QueryResponse(
                intent="clarification",
                response="I need a bit more information before I can help.",
                success=True,
                clarification_needed=True,
                clarifying_questions=plan.clarifying_questions,
                status_updates=["Need clarification"]
            )

        logger.info(f"Execution Plan: {', '.join([s.agent.value.upper() for s in plan.steps])}")
        try:
             status_updates.append("Plan created with steps: " + ", ".join([str(s.agent.value).upper() for s in plan.steps]))
        except:
             pass
             
        # 2. Execute Plan using Executor
        execution_result = {}
        async for event in plan_executor.execute_plan(
             plan, 
             context=f"{temporal_context}\nOriginal Query: {user_input}",
             sql_deps=sql_deps
        ):
            if event['type'] == 'status':
                status_updates.append(str(event['content']))
            elif event['type'] == 'result':
                execution_result = event['data']
        
        if not execution_result.get('success'):
             if execution_result.get('clarification_needed'):
                 return QueryResponse(
                    intent="clarification",
                    response="I need more info.", # execution shouldn't hit this if manager caught it, but fail-safe
                    success=True,
                    clarification_needed=True,
                    clarifying_questions=execution_result.get('questions')
                )
             raise Exception(execution_result.get('response', 'Unknown execution error'))

        final_response_text = execution_result['response']
        sql_data = execution_result.get('sql_data')

        # 3. Update Universal History
        conversation_history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
        conversation_history.append(ModelResponse(parts=[TextPart(content=final_response_text)]))
        conversation_history = keep_last_n_turns(conversation_history)

        total_duration = time.time() - start_time
        logger.info(f"--- 🏁 FINISH PROCESSING QUERY (Total: {total_duration:.2f}s) ---")

        return QueryResponse(
            intent="multi_agent",
            response=final_response_text,
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
    global conversation_history
    start_time = time.time()
    try:
        user_input = query.strip()
        if not user_input:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Query cannot be empty'})}\n\n"
            return
        
        logger.info(f"--- 📥 START STREAMING QUERY ---")
        logger.info(f"Query: '{user_input}'")
            
        yield f"data: {json.dumps({'type': 'status', 'content': 'Analyzing your request...'})}\n\n"
        
        # 1. Manager Agent Plan
        logger.info("🔍 Manager Agent: Starting Planning...")
        manager_start = time.time()
        
        temporal_context = get_temporal_context()
        from agents import manager_agent
        plan_result = await manager_agent.run(
            f"{temporal_context}{user_input}", 
            message_history=conversation_history
        )
        plan = plan_result.output
        manager_duration = time.time() - manager_start
        logger.info(f"✅ Manager Agent: Planning Complete ({manager_duration:.2f}s)")
        
        # Check for clarifying questions
        if plan.clarifying_questions:
             yield f"data: {json.dumps({'type': 'clarification', 'questions': plan.clarifying_questions})}\n\n"
             yield f"data: {json.dumps({'type': 'result', 'data': {'response': 'Please clarify: ' + ' '.join(plan.clarifying_questions)}})}\n\n"
             return

        logger.info(f"Execution Plan: {', '.join([s.agent.value.upper() for s in plan.steps])}")
        
        steps_desc = ", ".join([s.agent.value.upper() for s in plan.steps])
        yield f"data: {json.dumps({'type': 'status', 'content': f'Plan: {steps_desc}'})}\n\n"
        
        # Send the clean rewritten query to the UI
        yield f"data: {json.dumps({'type': 'rewritten_query', 'content': plan.rewritten_intent})}\n\n" 
        
        # 2. Execute Plan using Executor
        execution_result = {}
        async for event in plan_executor.execute_plan(
             plan, 
             context=f"{temporal_context}\nOriginal Query: {user_input}",
             sql_deps=sql_deps
        ):
            if event['type'] == 'status':
                yield f"data: {json.dumps({'type': 'status', 'content': event['content']})}\n\n"
            elif event['type'] == 'sql_query':
                yield f"data: {json.dumps({'type': 'sql_query', 'query': event['query'], 'explanation': event['explanation']})}\n\n"
            elif event['type'] == 'result':
                execution_result = event['data']
        
        final_response_text = execution_result.get('response', '')
        sql_data = execution_result.get('sql_data')
        
        # 3. Update History
        conversation_history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
        conversation_history.append(ModelResponse(parts=[TextPart(content=final_response_text)]))
        conversation_history = keep_last_n_turns(conversation_history)
        
        response_data = {
            "intent": "multi_agent",
            "response": final_response_text,
            "success": True,
            "sql_data": sql_data
        }
        yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"
        
        total_duration = time.time() - start_time
        logger.info(f"--- 🏁 FINISH STREAMING QUERY (Total: {total_duration:.2f}s) ---")

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
    global conversation_history
    conversation_history = []
    logger.info(f"🔄 Reset conversation history for session: {session_id}")
    return {"status": "success", "message": "Conversation history reset"}

@app.get("/schema")
async def get_schema():
    """Get the current database schema"""
    if sql_deps:
        return {
            "schema": sql_deps.schema_text,
            "status": "available" if sql_deps.schema_text != "Schema unavailable." else "unavailable",
            "approved_databases": sql_deps.approved_databases,
            "default_database": sql_deps.default_database
        }
    return {"schema": "Not initialized", "status": "unavailable"}

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
            "tldv": "ready",
            "general": "ready"
        },
        "mcp_servers": {
            "mysql": "connected",
            "calendar": "connected",
            "drive": "connected",
            "tldv": "connected"
        },
        "schema_loaded": sql_deps is not None and sql_deps.schema_text != "Schema unavailable."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
