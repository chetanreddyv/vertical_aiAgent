from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from fastapi.responses import StreamingResponse
import json
import time
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ModelRequest, UserPromptPart, ToolReturnPart
from pydantic_ai import UsageLimits
from langfuse import observe, Langfuse

import agents
from agents import (
    supervisor_agent, sql_agent, email_agent, calendar_agent,
    drive_agent, general_agent, jira_agent,
    mysql_mcp, calendar_mcp, drive_mcp, rag_mcp, jira_mcp, email_mcp,
    CONFIRMATION_MARKER, execute_confirmed_action,
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

# Global state
sessions: Dict[str, list] = {}
supervisor_deps: Optional[agents.SupervisorDeps] = None

security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    expected_username = os.getenv("BASIC_AUTH_USERNAME")
    expected_password = os.getenv("BASIC_AUTH_PASSWORD")
    if not expected_username or not expected_password:
        logger.error("Basic Auth credentials not set in environment variables")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication misconfigured",
        )
    is_correct_username = secrets.compare_digest(credentials.username, expected_username)
    is_correct_password = secrets.compare_digest(credentials.password, expected_password)
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def keep_last_n_turns(message_history: list, n_turns: int = 15) -> list:
    """Keep only the last N conversation turns."""
    max_messages = n_turns * 2
    if len(message_history) > max_messages:
        return message_history[-max_messages:]
    return message_history


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize agents and MCP servers on startup."""
    global supervisor_deps
    logger.info("Initializing MCP servers and fetching schema...")
    
    # Start MCP servers for all specialist agents
    async with (
        email_agent.run_mcp_servers(),
        sql_agent.run_mcp_servers(),
        calendar_agent.run_mcp_servers(),
        drive_agent.run_mcp_servers(),
        general_agent.run_mcp_servers(),
        jira_agent.run_mcp_servers(),
    ):
        try:
            supervisor_deps = await agents.initialize_agents()
            logger.info("✅ Agents initialized successfully")
        except Exception as e:
            logger.error(f"Error during initialization: {e}", exc_info=True)
            supervisor_deps = agents.SupervisorDeps(
                schema_text="Schema unavailable.",
                business_context="",
                approved_databases=["Salesforce"],
                default_database="Salesforce"
            )
        
        logger.info("✅ Supervisor ready")
        yield
        logger.info("Shutting down MCP servers...")


app = FastAPI(
    title="AI Agent API",
    description="FastAPI backend for multi-agent orchestration with MySQL & Google Workspace",
    version="2.0.0",
    lifespan=lifespan
)

# CORS middleware — set ALLOWED_ORIGINS=https://your-domain.com,https://other.com in production
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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
    sql_data: Optional[Dict[str, Any]] = None
    status_updates: Optional[list[str]] = None
    clarification_needed: Optional[bool] = False
    clarifying_questions: Optional[list[str]] = None

class StepConfirmation(BaseModel):
    session_id: str
    step_id: str  # action name (e.g., "send_email")
    approved_instruction: str  # JSON-encoded details
    approved_inputs: Optional[Dict[str, str]] = None

# In-memory store for pending confirmations
pending_confirmations: Dict[str, Dict[str, Any]] = {}


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "AI Agent API v2 (Supervisor)",
        "agents": ["email", "sql", "drive", "calendar", "jira", "general"]
    }

@app.get("/verify-auth")
async def verify_auth(username: str = Depends(check_auth)):
    return {"status": "authenticated", "user": username}


def _extract_confirmation_from_messages(messages) -> Optional[Dict]:
    """Scan agent messages for a confirmation payload in tool returns."""
    for msg in messages:
        if hasattr(msg, 'parts'):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    content = str(part.content)
                    if CONFIRMATION_MARKER in content:
                        try:
                            return json.loads(content)
                        except json.JSONDecodeError:
                            continue
    return None


@observe(capture_output=False)
async def query_stream_generator(query: str, session_id: str = "default"):
    """Process a query through the supervisor agent with SSE streaming."""
    global sessions, pending_confirmations
    start_time = time.time()
    try:
        user_input = query.strip()
        if not user_input:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Query cannot be empty'})}\n\n"
            return
        
        if session_id not in sessions:
            sessions[session_id] = []
        history = sessions[session_id]
        
        logger.info(f"--- 📥 START STREAMING QUERY ---")
        logger.info(f"Query: '{user_input}' (Session: {session_id})")
        
        yield f"data: {json.dumps({'type': 'status', 'content': 'Analyzing your request...'})}\n\n"
        
        # Run supervisor agent
        temporal_context = get_temporal_context()
        prompt = f"{temporal_context}{user_input}"
        
        result = await supervisor_agent.run(
            prompt,
            deps=supervisor_deps,
            message_history=history,
            usage_limits=UsageLimits(request_limit=25, tool_calls_limit=15),
        )
        
        # Check for confirmation requests in tool returns
        confirmation = _extract_confirmation_from_messages(result.all_messages())
        
        if confirmation and confirmation.get(CONFIRMATION_MARKER):
            # Store pending confirmation
            pending_confirmations[session_id] = {
                "action": confirmation["action"],
                "details": confirmation["details"],
                "preview": confirmation["preview"],
                "original_query": user_input,
            }
            
            action_name = confirmation["action"]
            preview_text = confirmation["preview"]
            detail_data = confirmation["details"]
            
            logger.info(f"⏸️ Confirmation required for {action_name}")
            
            # Send confirmation request to client
            status_msg = json.dumps({"type": "status", "content": f"Preparing {action_name}..."})
            yield f"data: {status_msg}\n\n"
            
            confirm_msg = json.dumps({
                "type": "confirmation_request",
                "step_id": action_name,
                "step_index": 0,
                "instruction": preview_text,
                "inputs": detail_data,
                "session_id": session_id,
            })
            yield f"data: {confirm_msg}\n\n"
            
            # Update history with the partial interaction
            approval_text = f"I've prepared the following action for your approval:\n\n{preview_text}"
            history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
            history.append(ModelResponse(parts=[TextPart(content=approval_text)]))
            sessions[session_id] = keep_last_n_turns(history)
            return
        
        # No confirmation needed — return the response directly
        response_text = str(result.output)
        
        # Update history
        history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
        history.append(ModelResponse(parts=[TextPart(content=response_text)]))
        sessions[session_id] = keep_last_n_turns(history)
        
        response_data = {
            "intent": "supervisor",
            "response": response_text,
            "success": True,
        }
        yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"
        
        total_duration = time.time() - start_time
        logger.info(f"--- 🏁 FINISH STREAMING QUERY (Total: {total_duration:.2f}s) ---")
    
    except Exception as e:
        logger.error(f"❌ Error processing query stream: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


@observe(capture_output=False)
async def confirm_step_stream_generator(confirmation: StepConfirmation):
    """Execute a confirmed write action."""
    global pending_confirmations, sessions
    
    session_id = confirmation.session_id
    pending = pending_confirmations.get(session_id)
    
    if not pending:
        yield f"data: {json.dumps({'type': 'error', 'error': 'No pending confirmation found for this session'})}\n\n"
        return
    
    action = pending["action"]
    details = pending["details"]
    
    # If user modified the instruction, try to parse updated details
    if confirmation.approved_inputs:
        details.update(confirmation.approved_inputs)
    
    logger.info(f"▶️ Executing confirmed action: {action}")
    yield f"data: {json.dumps({'type': 'status', 'content': f'Executing {action}...'})}\n\n"
    
    try:
        result = await execute_confirmed_action(action, details)
        
        # Clean up pending state
        del pending_confirmations[session_id]
        
        # Update history
        history = sessions.get(session_id, [])
        history.append(ModelResponse(parts=[TextPart(content=result)]))
        sessions[session_id] = keep_last_n_turns(history)
        
        response_data = {
            "intent": "confirmed_action",
            "response": result,
            "success": True,
        }
        yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"
    
    except Exception as e:
        logger.error(f"❌ Error executing confirmed action: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


@app.get("/query-stream")
async def stream_query(query: str, session_id: str = "default", username: str = Depends(check_auth)):
    return StreamingResponse(
        query_stream_generator(query, session_id),
        media_type="text/event-stream"
    )

@app.post("/confirm-step")
async def confirm_step(confirmation: StepConfirmation, username: str = Depends(check_auth)):
    return StreamingResponse(
        confirm_step_stream_generator(confirmation),
        media_type="text/event-stream"
    )

@app.post("/query", response_model=QueryResponse)
@observe()
async def process_query(request: QueryRequest, username: str = Depends(check_auth)):
    """Non-streaming query endpoint."""
    global sessions
    start_time = time.time()
    try:
        user_input = request.query.strip()
        if not user_input:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        session_id = request.session_id or "default"
        if session_id not in sessions:
            sessions[session_id] = []
        history = sessions[session_id]
        
        temporal_context = get_temporal_context()
        result = await supervisor_agent.run(
            f"{temporal_context}{user_input}",
            deps=supervisor_deps,
            message_history=history,
            usage_limits=UsageLimits(request_limit=25, tool_calls_limit=15),
        )
        
        response_text = str(result.output)
        
        history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
        history.append(ModelResponse(parts=[TextPart(content=response_text)]))
        sessions[session_id] = keep_last_n_turns(history)
        
        return QueryResponse(
            intent="supervisor",
            response=response_text,
            success=True,
            status_updates=["Completed"]
        )
    
    except Exception as e:
        logger.error(f"❌ Error processing query: {e}", exc_info=True)
        return QueryResponse(
            intent="error",
            response="",
            success=False,
            error=str(e)
        )


@app.post("/reset-history")
async def reset_history(session_id: str = "default", username: str = Depends(check_auth)):
    """Clear conversation history and pending confirmations."""
    global sessions, pending_confirmations
    if session_id in sessions:
        sessions[session_id] = []
    if session_id in pending_confirmations:
        del pending_confirmations[session_id]
    logger.info(f"🗑️ Session {session_id} cleared")
    return {"status": "success", "message": f"History cleared for session {session_id}"}

@app.get("/schema")
async def get_schema():
    if supervisor_deps:
        return {
            "schema": supervisor_deps.schema_text,
            "status": "available" if supervisor_deps.schema_text != "Schema unavailable." else "unavailable",
            "approved_databases": supervisor_deps.approved_databases,
            "default_database": supervisor_deps.default_database
        }
    return {"schema": "Not initialized", "status": "unavailable"}

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agents": {
            "supervisor": "ready",
            "email": "ready",
            "sql": "ready",
            "drive": "ready",
            "calendar": "ready",
            "jira": "ready",
            "general": "ready"
        },
        "mcp_servers": {
            "mysql": "connected",
            "calendar": "connected",
            "drive": "connected",
            "jira": "connected",
            "rag": "connected"
        },
        "schema_loaded": supervisor_deps is not None and supervisor_deps.schema_text != "Schema unavailable."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
