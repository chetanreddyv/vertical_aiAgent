# sql agent blows the context because the sql context is passed with every turn in the system prompt in conversation history. 
# clean up the agents orchestration
 - manager agent 
 - sql agent
 - email agent
 - calendar agent
 - drive agent
 - docs agent
 - tldv agent
 
Manager Agent:
 - steps
 - rewritten intent
 - final response instruction

 Build the missing executor
Implement execute_plan(plan: ExecutionPlan) that runs steps sequentially, captures each step output, and produces a final synthesized response.
​

Add deterministic dependency passing (e.g., templating like {{step_1.result}}), so Step 2 can safely consume Step 1 output instead of “wait for previous result” text.
​

Add an explicit confirmation gate in the executor for side effects (send email, create/update calendar events, delete/move files), rather than relying only on agent prompts.
​

Fix tool capability drift
Either attach a real Docs MCP server to docs_agent or remove/rename it so prompts don’t claim capabilities it doesn’t have.
​

Rename tldv_agent vs rag_mcp to reflect reality (or swap in an actual TLDV MCP server) so routing and debugging align with the actual backend.
​

Make MCP lifecycle explicit
Start/stop MCP stdio servers using async context management (e.g., async with server:) to avoid orphan subprocesses and flaky connections.
​

Standardize env propagation so deploys are predictable (be explicit about which env vars are passed into each MCP server process).
​

Harden MCP logging and transport
Ensure every MCP stdio server never writes non-protocol output to stdout (no print()), and sends logs to stderr instead.
​

Add structured error handling + retries around tool calls (with backoff) so transient MCP/tool failures don’t break the whole run.
​

Add guardrails (cost + runaway control)
Apply UsageLimits on agent runs (especially manager + tool-heavy specialists) using request_limit and tool_calls_limit to prevent runaway loops and cap tool execution.
​

Add timeouts per step (tool timeout + overall plan timeout) and return partial results with clear failure reporting when limits are hit.
Based on your current setup and the specific failure mode you described (the "Happy Talk" problem where the agent claims to have performed an action without calling the tool), here is a robust implementation plan and the necessary code logic.

### The Core Problem: "Planning vs. Doing" Disconnect
Your current architecture trusts the text output of the specialist agent too much.
*   **Current State:** Manager -> Plan -> Loop -> `agent.run()` -> **Result (Text)** -> "Success"
*   **The Bug:** `email_agent` returns "I have sent the email." (Text). The loop sees a successful return and moves on. The `send_email` tool was never triggered.

### The Solution: Validated Execution Loop with Context Injection
We need to replace the simple loop with a dedicated **Orchestrator/Executor** function that:
1.  **Injects Context:** Passes outputs from Step 1 into the inputs of Step 2.
2.  **Enforces Tool Usage:** Verifies that "Action Agents" (Email, Calendar, SQL) actually called a tool before marking the step as complete.
3.  **Retries:** If an agent hallucinates an action without doing it, the Executor rejects the response and forces a retry.

***

### Implementation Plan

#### 1. Define a `StepResult` Container
We need a structured way to capture what happened in previous steps so the next agent knows what to do.

```python
@dataclass
class StepResult:
    step_id: int
    agent_name: str
    instruction: str
    output: str  # The text response
    tool_calls: list[str]  # Names of tools called
    success: bool
```

#### 2. The `Smart Executor` Logic
This is the core fix. It runs the plan, manages history, and creates a "feedback loop" if an agent fails to perform an action.

**Key Logic Change:** We will not just pass the `instruction`. We will wrap the instruction in a `Contextual Prompt` that includes the results of previous steps.

#### 3. Revised Code Implementation

Here is the code you should add/modify in your orchestration layer.

```python
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolReturn
import json

async def execute_plan(plan: ExecutionPlan, user_query: str, sql_deps: SqlDeps) -> str:
    """
    Executes the plan sequentially, passing context between agents and verifying tool usage.
    """
    logger.info(f"🚀 Starting Execution Plan: {len(plan.steps)} steps")
    
    # 1. Context Container
    step_results: list[StepResult] = []
    global_context = f"User Original Query: {user_query}\n\n"

    for i, step in enumerate(plan.steps):
        logger.info(f"▶️ Executing Step {i+1}/{len(plan.steps)}: [{step.agent}] {step.instruction}")
        
        # 2. Select the Agent
        current_agent = get_agent_by_name(step.agent) # Helper function to map enum to object
        current_deps = sql_deps if step.agent == AgentSelection.SQL else None

        # 3. Construct Contextual Prompt
        # We append previous results so the agent knows what happened before
        context_block = ""
        if step_results:
            context_block = "\n\n--- PREVIOUS STEPS CONTEXT ---\n"
            for res in step_results:
                context_block += f"Step {res.step_id} ({res.agent_name}): {res.output}\n"
            context_block += "------------------------------\n"
            context_block += "Use the context above to fulfill the current instruction.\n"

        full_prompt = f"{step.instruction}{context_block}"

        # 4. Execute with Retry Logic for "Action Validation"
        max_retries = 2
        step_success = False
        final_response = ""
        tools_used = []

        for attempt in range(max_retries):
            try:
                # Run the agent
                result = await current_agent.run(full_prompt, deps=current_deps)
                final_response = result.data
                
                # Inspect Tool Usage (The Fix)
                # Pydantic AI captures messages. We check if tool calls occurred.
                # Note: This logic depends on Pydantic AI version, assuming result.messages contains history
                tools_used = [
                    m.parts[0].tool_name 
                    for m in result.new_messages() 
                    if hasattr(m, 'parts') and hasattr(m.parts[0], 'tool_name')
                ]

                # VALIDATION LOGIC:
                # If the agent is an "Action Agent" and specific keywords exist, enforce tool use.
                if is_action_required(step.agent, step.instruction) and not tools_used:
                    logger.warning(f"⚠️ Step {i+1} Output: '{final_response}' but NO tools were called. Retrying...")
                    full_prompt += (
                        "\n\nSYSTEM ERROR: You responded with text but did NOT call the required tool. "
                        "You must execute the tool (e.g., send_email, execute_query) directly. "
                        "Do not just say you will do it."
                    )
                    continue # Retry loop
                
                step_success = True
                break # Success

            except Exception as e:
                logger.error(f"❌ Step {i+1} Failed: {e}")
                full_prompt += f"\n\nSYSTEM ERROR: The previous attempt failed with error: {str(e)}. Try again."

        # 5. Store Result
        step_results.append(StepResult(
            step_id=i+1,
            agent_name=step.agent,
            instruction=step.instruction,
            output=final_response,
            tool_calls=tools_used,
            success=step_success
        ))

        if not step_success:
            return f"Execution halted at step {i+1}. The agent failed to perform the action."

    # 6. Final Synthesis
    # Optionally, ask the manager to summarize the final results
    return f"Execution Complete. Final Outcome: {step_results[-1].output}"

def get_agent_by_name(name: AgentSelection):
    mapping = {
        "email": email_agent,
        "sql": sql_agent,
        "drive": drive_agent,
        "calendar": calendar_agent,
        "tldv": tldv_agent,
        "general": general_agent
    }
    return mapping.get(name)

def is_action_required(agent_enum: AgentSelection, instruction: str) -> bool:
    """
    Heuristic to decide if we should enforce tool usage.
    """
    # SQL always requires a query
    if agent_enum == AgentSelection.SQL:
        return True
        
    # Email/Calendar usually require action unless it's just "checking"
    action_keywords = ["send", "create", "update", "delete", "upload", "schedule"]
    if agent_enum in [AgentSelection.EMAIL, AgentSelection.CALENDAR, AgentSelection.DRIVE]:
        if any(kw in instruction.lower() for kw in action_keywords):
            return True
            
    return False
```

### 4. Refine Agent Prompts
The "Executor" fixes the logic, but better prompts reduce the retries. Update your `email_agent` (and others) to be explicitly "Tool-First".

**Updated Email Agent Prompt:**
```python
email_agent = Agent(
    "google-gla:gemini-3-flash-preview",
    system_prompt=(
        "You are a Gmail automation assistant.\n"
        "RULES:\n"
        "1. If the user instruction says 'Send email', you must call the `send_email` tool. "
        "   Do NOT return text saying 'I will send it'. Call the tool.\n"
        "2. If you need to reply, search for the original email ID first.\n"
        "3. Output strictly what the tool returns."
    ),
    mcp_servers=[email_mcp]
)
```

### 5. Why This Works
1.  **Context Passing:** `step_results` ensures that if Step 1 finds a meeting summary, Step 2 (Email) sees that summary in its `full_prompt`.
2.  **Validation:** The `is_action_required` check catches the "Happy Talk" failure. If the plan says "Send email" and the agent returns text without `tools_used`, the loop forces it to try again with a stricter prompt.
3.  **Dependency Injection:** It correctly handles `sql_deps` only for the SQL agent while passing `None` to others.

### Next Steps for You
1.  Copy the `StepResult` dataclass and `execute_plan` function into your main file.
2.  Replace your current `for step in plan.steps:` loop with a call to `await execute_plan(...)`.
3.  Verify your `pydantic_ai` version supports `result.new_messages()` to inspect tool calls (standard in v0.0.18+). If using an older version, inspect `result.usage` or the raw messages.