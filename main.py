from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
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
from langfuse import observe, Langfuse

# Import the client agents and utilities
import agents

from agents import (
    manager_agent, email_agent, sql_agent, drive_agent, calendar_agent, 
    manager_agent, email_agent, sql_agent, drive_agent, calendar_agent, 
    tldv_agent, jira_agent, general_agent, mysql_mcp, calendar_mcp, 
    drive_mcp, rag_mcp, jira_mcp, AgentSelection
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

security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    # Read from environment variables - FAIL if not set for security
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
        "jira": jira_agent,
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
        tldv_agent.run_mcp_servers(),
        jira_agent.run_mcp_servers()
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
            "jira": jira_agent,
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
# IMPORTANT: When allow_credentials is True, allow_origins cannot be ["*"]
origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response models
# In-memory store for paused executions
paused_states: Dict[str, Dict[str, Any]] = {}

class StepConfirmation(BaseModel):
    session_id: str
    step_id: str
    approved_instruction: str
    approved_inputs: Optional[Dict[str, str]] = None

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
        "agents": ["email", "sql", "drive", "calendar", "tldv", "jira", "general"]
    }

@app.get("/verify-auth")
async def verify_auth(username: str = Depends(check_auth)):
    """Simple endpoint to verify credentials"""
    return {"status": "authenticated", "user": username}

@app.post("/query", response_model=QueryResponse)
@observe()
async def process_query(request: QueryRequest, username: str = Depends(check_auth)):
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
            
            clarification_response = "I need a bit more information before I can help."
            
            # Update History
            conversation_history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
            conversation_history.append(ModelResponse(parts=[TextPart(content=clarification_response)]))
            conversation_history = keep_last_n_turns(conversation_history)

            return QueryResponse(
                intent="clarification",
                response=clarification_response,
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

@observe(capture_output=False)
async def query_stream_generator(query: str):
    """Generate status updates during query processing"""
    global conversation_history, paused_states
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
             
             clarification_response = 'Please clarify: ' + '\n'.join(plan.clarifying_questions)
             
             # Structure result correctly so UI displays it as assistant message
             response_data = {
                 "intent": "clarification",
                 "response": clarification_response,
                 "success": True,
                 "clarification_needed": True,
                 "questions": plan.clarifying_questions
             }
             yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"
             
             # CRITICAL: Update History so context isn't lost
             conversation_history.append(ModelRequest(parts=[UserPromptPart(content=user_input)]))
             conversation_history.append(ModelResponse(parts=[TextPart(content=clarification_response)]))
             conversation_history = keep_last_n_turns(conversation_history)
             return

        logger.info(f"Execution Plan: {', '.join([s.agent.value.upper() for s in plan.steps])}")
        
        steps_desc = ", ".join([s.agent.value.upper() for s in plan.steps])
        yield f"data: {json.dumps({'type': 'status', 'content': f'Plan: {steps_desc}'})}\n\n"
        
        # Send the clean rewritten query to the UI
        yield f"data: {json.dumps({'type': 'rewritten_query', 'content': plan.rewritten_intent})}\n\n" 
        
        session_id = str(int(time.time())) # Simple session ID generation if not provided
        
        # 2. Execute Plan using Executor
        execution_result = {}
        async for event in plan_executor.execute_plan(
             plan, 
             context=f"{temporal_context}\nOriginal Query: {user_input}",
             sql_deps=sql_deps
        ):
            if event['type'] == 'status':
                yield f"data: {json.dumps({'type': 'status', 'content': event['content']})}\n\n"
            elif event['type'] == 'step_start':
                yield f"data: {json.dumps(event)}\n\n"
            elif event['type'] == 'sql_query':
                yield f"data: {json.dumps({'type': 'sql_query', 'query': event['query'], 'explanation': event['explanation']})}\n\n"
            elif event['type'] == 'result':
                execution_result = event['data']
            elif event['type'] == 'confirmation_request':
                # Pause execution!
                paused_states[session_id] = {
                    "plan": plan,
                    "context": f"{temporal_context}\nOriginal Query: {user_input}",
                    "step_index": event['step_index']
                }
                logger.info(f"⏸️ Paused execution for session {session_id} at step {event['step_index']}")
                
                # Yield confirmation event with session_id
                event['session_id'] = session_id
                yield f"data: {json.dumps(event)}\n\n"
                return # End stream for now, client will resume via /confirm-step
        
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

@observe(capture_output=False)
async def confirm_step_stream_generator(confirmation: StepConfirmation):
    """Resume execution after confirmation"""
    global paused_states, conversation_history
    
    session_id = confirmation.session_id
    state = paused_states.get(session_id)
    
    if not state:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Session expired or not found'})}\n\n"
        return

    plan = state['plan']
    context = state['context']
    step_index = state['step_index']
    
    # Update the step with approved details
    step = plan.steps[step_index]
    step.instruction = confirmation.approved_instruction
    if confirmation.approved_inputs:
        step.inputs = confirmation.approved_inputs
    
    # Mark as confirmed by setting requires_confirmation to False
    # This ensures execute_plan doesn't pause again on this step
    step.requires_confirmation = False
    
    logger.info(f"▶️ Resuming session {session_id} from step {step_index} with updated instruction")
    
    try:
        execution_result = {}
        # Resume execution from the paused index
        async for event in plan_executor.execute_plan(
             plan, 
             context=context,
             sql_deps=sql_deps,
             start_step_index=step_index
        ):
            if event['type'] == 'status':
                yield f"data: {json.dumps({'type': 'status', 'content': event['content']})}\n\n"
            elif event['type'] == 'step_start':
                yield f"data: {json.dumps(event)}\n\n"
            elif event['type'] == 'sql_query':
                yield f"data: {json.dumps({'type': 'sql_query', 'query': event['query'], 'explanation': event['explanation']})}\n\n"
            elif event['type'] == 'result':
                execution_result = event['data']
            elif event['type'] == 'confirmation_request':
                # Encounters another confirmation request later in the plan
                paused_states[session_id] = {
                    "plan": plan,
                    "context": context,
                    "step_index": event['step_index']
                }
                event['session_id'] = session_id
                yield f"data: {json.dumps(event)}\n\n"
                return

        # Cleanup state after successful completion
        if session_id in paused_states:
            del paused_states[session_id]

        final_response_text = execution_result.get('response', '')
        sql_data = execution_result.get('sql_data')
        
        # Update History
        conversation_history.append(ModelResponse(parts=[TextPart(content=final_response_text)]))
        conversation_history = keep_last_n_turns(conversation_history)
        
        response_data = {
            "intent": "multi_agent",
            "response": final_response_text,
            "success": True,
            "sql_data": sql_data
        }
        yield f"data: {json.dumps({'type': 'result', 'data': response_data})}\n\n"

    except Exception as e:
        logger.error(f"❌ Error resuming execution: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

@app.get("/query-stream")
async def stream_query(query: str, username: str = Depends(check_auth)):
    return StreamingResponse(
        query_stream_generator(query),
        media_type="text/event-stream"
    )

@app.post("/confirm-step")
async def confirm_step(confirmation: StepConfirmation, username: str = Depends(check_auth)):
    return StreamingResponse(
        confirm_step_stream_generator(confirmation),
        media_type="text/event-stream"
    )

@app.post("/reset-history")
async def reset_history(username: str = Depends(check_auth)):
    """Clear conversation history and paused execution states"""
    global conversation_history, paused_states
    conversation_history.clear()
    paused_states.clear()
    logger.info("🗑️ Conversation history and paused states cleared")
    return {"status": "success", "message": "History cleared"}

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
            "jira": "ready",
            "general": "ready"
        },
        "mcp_servers": {
            "mysql": "connected",
            "calendar": "connected",
            "drive": "connected",
            "jira": "connected",
            "tldv": "connected"
        },
        "schema_loaded": sql_deps is not None and sql_deps.schema_text != "Schema unavailable."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
