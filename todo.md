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
