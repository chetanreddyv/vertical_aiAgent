from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
import time
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ModelRequest, UserPromptPart

# Import the client agents and utilities
import agents

from agents import (
    manager_agent, email_agent, sql_agent, drive_agent, calendar_agent, 
    docs_agent, tldv_agent, general_agent, mysql_mcp, calendar_mcp, 
    drive_mcp, tldv_mcp, AgentSelection
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

# Global universal conversation history
conversation_history = []
formatted_schema = ""


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
        "docs": docs_agent,
        "tldv": tldv_agent,
        "general": general_agent
    }
    return agents_map.get(name, general_agent)

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
        drive_agent.run_mcp_servers(),
        tldv_agent.run_mcp_servers()
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
        "agents": ["email", "sql", "drive", "calendar", "docs", "tldv", "general"]
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
        logger.info(f"Execution Plan: {', '.join([s.agent.value.upper() for s in plan.steps])}")
        
        status_updates.append("Plan created with steps: " + ", ".join([str(s.agent.value).upper() for s in plan.steps]))
        
        # 2. Execute Steps Sequentially
        step_results = []
        sql_data = None
        
        for i, step in enumerate(plan.steps):
            agent_name = step.agent.value
            status_updates.append(f"Step {i+1}: Executing {agent_name.upper()} Agent...")
            
            logger.info(f"🔄 Step {i+1} START: {agent_name.upper()}")
            logger.info(f"Instruction: {step.instruction}")
            step_start = time.time()
            
            # Context for the specific step
            previous_steps_context = ""
            if step_results:
                previous_steps_context = "\n\nCONTEXT FROM PREVIOUS STEPS:\n" + "\n".join(step_results)
            
            agent_input = f"{temporal_context}TASK: {step.instruction}\n{previous_steps_context}"
            agent = get_agent_by_name(agent_name)
            
            # Run the agent
            result = await agent.run(agent_input)
            
            step_output = str(result.output)
            step_duration = time.time() - step_start
            logger.info(f"✅ Step {i+1} COMPLETE: {agent_name.upper()} ({step_duration:.2f}s)")
            logger.info(f"📄 Result from {agent_name.upper()}: {step_output[:500]}..." if len(step_output) > 500 else f"📄 Result from {agent_name.upper()}: {step_output}")
            
            step_results.append(f"--- Result from {agent_name} ---\n{step_output}")
            
            # Capture SQL data if present
            if hasattr(result.output, 'sqlquery'):
                query = result.output.sqlquery
                database = getattr(result.output, 'database', None) or "Salesforce"
                status_updates.append(f"Executing database query on {database}...")
                
                logger.info(f"🗄️ Executing SQL on {database}...")
                sql_start = time.time()
                query_result = await mysql_mcp.direct_call_tool(
                    name="execute_query",
                    args={"query": query, "database": database, "read_only": True}
                )
                sql_duration = time.time() - sql_start
                logger.info(f"✅ SQL Execution Complete ({sql_duration:.2f}s)")
                
                formatted_results = format_query_results(query_result)
                step_results[-1] += f"\n\nResults:\n{formatted_results}"
                if query_result.get('success'):
                    sql_data = query_result
                    sql_data['executed_query'] = query
                    sql_data['executed_explanation'] = getattr(result.output, 'explanation', 'SQL Query')

        # 3. Final Synthesis
        status_updates.append("Synthesizing final response...")
        logger.info("🧠 Synthesis: Generating final response...")
        synthesis_start = time.time()
        
        final_context = f"Original Query: {user_input}\n\nExecution Results:\n" + "\n".join(step_results)
        final_context += f"\n\nInstruction: {plan.final_response_instruction}"
        
        final_result = await agents.general_agent.run(final_context)
        final_response_text = str(final_result.output)
        synthesis_duration = time.time() - synthesis_start
        logger.info(f"✅ Synthesis Complete ({synthesis_duration:.2f}s)")
        logger.info(f"💬 Final Response: {final_response_text[:200]}...")

        status_updates.append("Response generated")
        
        # 4. Update Universal History
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
        logger.info(f"Execution Plan: {', '.join([s.agent.value.upper() for s in plan.steps])}")
        
        steps_desc = ", ".join([s.agent.value.upper() for s in plan.steps])
        yield f"data: {json.dumps({'type': 'status', 'content': f'Plan: {steps_desc}'})}\n\n"
        
        # Send the clean rewritten query to the UI
        yield f"data: {json.dumps({'type': 'rewritten_query', 'content': plan.rewritten_intent})}\n\n" 
        
        # 2. Execute Steps
        step_results = []
        sql_data = None
        
        for i, step in enumerate(plan.steps):
            agent_name = step.agent.value
            yield f"data: {json.dumps({'type': 'status', 'content': f'Step {i+1}: {agent_name.title()} Agent working...'})}\n\n"
            
            logger.info(f"🔄 Step {i+1} START: {agent_name.upper()}")
            logger.info(f"Instruction: {step.instruction}")
            step_start = time.time()
            
            previous_steps_context = ""
            if step_results:
                previous_steps_context = "\n\nCONTEXT FROM PREVIOUS STEPS:\n" + "\n".join(step_results)
            
            agent_input = f"{temporal_context}TASK: {step.instruction}\n{previous_steps_context}"
            agent = get_agent_by_name(agent_name)
            
            result = await agent.run(agent_input)
            step_output = str(result.output)
            step_duration = time.time() - step_start
            logger.info(f"✅ Step {i+1} COMPLETE: {agent_name.upper()} ({step_duration:.2f}s)")
            logger.info(f"📄 Result from {agent_name.upper()}: {step_output[:500]}..." if len(step_output) > 500 else f"📄 Result from {agent_name.upper()}: {step_output}")
            
            step_results.append(f"--- Result from {agent_name} ---\n{step_output}")
            
            # Handle SQL specific UI
            if hasattr(result.output, 'sqlquery'):
                query = result.output.sqlquery
                explanation = getattr(result.output, 'explanation', 'SQL Query')
                database = getattr(result.output, 'database', None) or "Salesforce"
                
                yield f"data: {json.dumps({'type': 'sql_query', 'query': query, 'explanation': explanation})}\n\n"
                yield f"data: {json.dumps({'type': 'status', 'content': f'Executing database query on {database}...'})}\n\n"
                
                logger.info(f"🗄️ Executing SQL on {database}...")
                sql_start = time.time()
                query_result = await mysql_mcp.direct_call_tool(
                    name="execute_query",
                    args={"query": query, "database": database, "read_only": True}
                )
                sql_duration = time.time() - sql_start
                logger.info(f"✅ SQL Execution Complete ({sql_duration:.2f}s)")
                
                formatted_results = format_query_results(query_result)
                step_results[-1] += f"\n\nResults:\n{formatted_results}"
                if query_result.get('success'):
                    sql_data = query_result
                    sql_data['executed_query'] = query
                    sql_data['executed_explanation'] = explanation
                    
        # 3. Final Synthesis
        yield f"data: {json.dumps({'type': 'status', 'content': 'Synthesizing final response...'})}\n\n"
        logger.info("🧠 Synthesis: Generating final response...")
        synthesis_start = time.time()
        
        final_context = f"Original Query: {user_input}\n\nExecution Results:\n" + "\n".join(step_results)
        final_context += f"\n\nInstruction: {plan.final_response_instruction}"
        
        final_result = await agents.general_agent.run(final_context)
        final_response_text = str(final_result.output)
        synthesis_duration = time.time() - synthesis_start
        logger.info(f"✅ Synthesis Complete ({synthesis_duration:.2f}s)")
        logger.info(f"💬 Final Response: {final_response_text[:200]}...")
        
        # 4. Update History
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
            "tldv": "ready",
            "general": "ready"
        },
        "mcp_servers": {
            "mysql": "connected",
            "calendar": "connected",
            "drive": "connected",
            "tldv": "connected"
        },
        "schema_loaded": formatted_schema != "Schema unavailable."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
