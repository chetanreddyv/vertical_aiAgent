
import logging
import re
from typing import Dict, Any, List, Optional
from agents import ExecutionPlan, Step, Agent, AgentSelection,  mysql_mcp
from pydantic_ai import UsageLimits
import time
from utils import format_query_results

logger = logging.getLogger(__name__)

class PlanExecutor:
    def __init__(self, agents_map: Dict[str, Agent]):
        self.agents_map = agents_map
        self.step_outputs: Dict[str, str] = {}
        self.sql_data: Optional[Dict[str, Any]] = None

    def resolve_inputs(self, text: str) -> str:
        """
        Replace template variables like {{steps.step_id.output}} with actual values.
        """
        if not text:
            return ""
        
        # Regex to find {{steps.STEP_ID.output}}
        # We allow whitespace inside the braces
        pattern = r"\{\{\s*steps\.([a-zA-Z0-9_]+)\.output\s*\}\}"
        
        def replace_match(match):
            step_id = match.group(1)
            # If step output not found, keep the template or replace with empty? 
            # Keeping it might verify it failed. Let's replace with empty string or warning if missing.
            if step_id in self.step_outputs:
                return str(self.step_outputs[step_id])
            else:
                logger.warning(f"Reference to missing step output: {step_id}")
                return f"[MISSING OUTPUT FOR {step_id}]"

        return re.sub(pattern, replace_match, text)

    def resolve_step_inputs(self, step: Step) -> str:
        """
        Resolve the instruction and input dictionary for a step.
        Returns the fully resolved prompt for the agent.
        """
        # Resolve variables in the instruction itself
        resolved_instruction = self.resolve_inputs(step.instruction)
        
        # Append explicit inputs if any
        if step.inputs:
            resolved_instruction += "\n\nINPUT CONTEXT:"
            for key, val in step.inputs.items():
                resolved_val = self.resolve_inputs(val)
                resolved_instruction += f"\n{key}: {resolved_val}"
        
        return resolved_instruction

    async def execute_step(self, step: Step, context: str, sql_deps=None) -> Any:
        """
        Execute a single step using the appropriate agent.
        """
        agent_name = step.agent.value
        agent = self.agents_map.get(agent_name)
        if not agent:
            raise ValueError(f"Unknown agent: {agent_name}")

        # Resolve inputs using outputs from previous steps
        # We combine the global context (temporal etc) with the step-specific resolved instruction
        resolved_instruction = self.resolve_step_inputs(step)
        full_prompt = f"{context}\nTASK: {resolved_instruction}"
        
        # Add context from *all* previous steps if not explicitly templated?
        # The prompt says "Must include all necessary context". 
        # But `step.inputs` implies we should rely on explicit passing.
        # However, to be safe and robust (manager might forget), we can optionally append history 
        # BUT the new design aims for deterministic dependency passing.
        # Let's stick to what the manager defined, plus the resolved instruction.
        
        logger.info(f"🚀 Executing Step {step.id} ({agent_name}): {resolved_instruction[:100]}...")
        
        usage_limits = UsageLimits(request_limit=10, tool_calls_limit=5)
        
        if agent_name == "sql":
            result = await agent.run(full_prompt, deps=sql_deps, usage_limits=usage_limits)
        else:
            result = await agent.run(full_prompt, usage_limits=usage_limits)
            
        return result

    async def execute_plan(self, plan: ExecutionPlan, context: str, sql_deps=None, start_step_index: int = 0):
        """
        Execute the entire plan sequentially.
        Yields events: 
        - {'type': 'status', 'content': str}
        - {'type': 'sql_query', 'query': str, 'explanation': str}
        - {'type': 'result', 'data': dict}
        - {'type': 'clarification', 'questions': list}
        """
        if start_step_index == 0:
            self.step_outputs = {}
            self.sql_data = None
        results_summary = []

        # 1. Check for clarifying questions
        if start_step_index == 0 and plan.clarifying_questions:
            logger.info(f"❓ Plan requires clarification: {plan.clarifying_questions}")
            yield {
                "type": "clarification", 
                "questions": plan.clarifying_questions
            }
            yield {
                "type": "result",
                "data": {
                    "success": False,
                    "clarification_needed": True,
                    "questions": plan.clarifying_questions
                }
            }
            return

        # 2. Execute Steps
        start_index = start_step_index
        
        for i, step in enumerate(plan.steps):
            # Skip steps that were already executed
            if i < start_index:
                continue

            # Resolve placeholders for frontend display
            resolved_instruction_for_ui = self.resolve_inputs(step.instruction)
            resolved_inputs_for_ui = {}
            if step.inputs:
                for k, v in step.inputs.items():
                    resolved_inputs_for_ui[k] = self.resolve_inputs(v)

            # Yield detailed step start event
            yield {
                "type": "step_start",
                "step_id": step.id,
                "agent": step.agent.value,
                "content": f"Step {i+1}: {step.agent.value.title()} Agent working...",
                "instruction": resolved_instruction_for_ui
            }
            
            # Check for confirmation requirement
            if step.requires_confirmation:
                # Check if this step was just confirmed (passed in via confirm_step arg or similar logic?)
                # Actually, the caller will handle resuming. We just need to stop if we hit a confirmation 
                # that hasn't been "resolved" (which effectively means we just pause here).
                # But wait, if we are resuming, we are given the *edited* plan for this step.
                # So if we are resuming at step i, we assume the caller has already addressed the confirmation 
                # by potentially modifying the step in the plan passed to this function.
                # However, we need a way to know if we should pause *now*.
                # If we are starting at step i (resuming), we proceed. 
                # If we encounter a *later* step j > i that needs confirmation, we pause there.
                
                # Logic: If this is the *first* step we are executing in this run, 
                # we assume it's approved (or doesn't need approval if start_index=0 and req_conf=False).
                # If we hit a requires_confirmation step *after* the start, we pause.
                
                # Wait, what if the first step needs confirmation? 
                # The Manager creates the plan. Step 1 has req_conf=True.
                # We start at index 0. We see req_conf=True. We must PAUSE.
                # The user confirms. We call execute_plan with start_index=0 again (but with modified plan?).
                # If we just call it again, we hit the same check.
                # We need a way to say "treat this step as confirmed".
                # Simplest way: The '/confirm-step' endpoint will likely *update* the step's requires_confirmation to False
                # OR we just rely on the fact that if we are resuming, the user explicitly triggered it.
                
                # Let's add a `force_execution` set or similar? Or simpler:
                # If we are resuming (i == start_index) AND the step was the reason we paused, we assume it's go time?
                # No, that's risky.
                
                # Better: The `/confirm-step` logic in main.py will update the `requires_confirmation` flag to False 
                # in the *copy* of the plan it submits for execution.
                # So here, we just check the flag. If it's True, we pause.
                
                logger.info(f"⚠️ Step {step.id} requires confirmation: {resolved_instruction_for_ui}")
                yield {
                    "type": "confirmation_request",
                    "step_id": step.id,
                    "step_index": i,
                    "instruction": resolved_instruction_for_ui,
                    "inputs": resolved_inputs_for_ui
                }
                return # Stop execution here. State is preserved in main.py

            try:
                result = await self.execute_step(step, context, sql_deps)
                output_str = str(result.output)
                
                # Store output for future steps
                self.step_outputs[step.id] = output_str
                results_summary.append(f"STEP {step.id} RESULT ({step.agent.value}):\n{output_str}")
                
                 # Handle SQL specific logic (execution and formatting)
                if hasattr(result.output, 'sqlquery'):
                    query = result.output.sqlquery
                    explanation = getattr(result.output, 'explanation', 'SQL Query')
                    database = getattr(result.output, 'database', None) or "Salesforce"
                    
                    yield {"type": "sql_query", "query": query, "explanation": explanation}
                    yield {"type": "status", "content": f"Executing database query on {database}..."}
                    
                    logger.info(f"🗄️ Executing SQL on {database}...")
                    query_result = await mysql_mcp.direct_call_tool(
                        name="execute_query",
                        args={"query": query, "database": database, "read_only": True}
                    )
                    
                    formatted_results = format_query_results(query_result)
                    
                    # update the step output to include the actual SQL results so subsequent steps see data, not just the query
                    full_sql_output = f"QUERY: {query}\n\nEXPLANATION: {explanation}\n\nRESULTS:\n{formatted_results}"
                    self.step_outputs[step.id] = full_sql_output
                    results_summary[-1] = f"STEP {step.id} RESULT ({step.agent.value}):\n{full_sql_output}"

                    if query_result.get('success'):
                        self.sql_data = query_result
                        self.sql_data['executed_query'] = query
                        self.sql_data['executed_explanation'] = explanation

            except Exception as e:
                logger.error(f"❌ Step {step.id} failed: {e}", exc_info=True)
                if plan.error_policy == "fail_fast":
                     raise e
                elif plan.error_policy == "skip":
                    self.step_outputs[step.id] = f"Error: {str(e)}"
                    yield {"type": "status", "content": f"Step {step.id} failed but policy is 'skip'. Moving on..."}
                    continue
                else: 
                     # Default to continue/retry logic or just capture error
                     self.step_outputs[step.id] = f"Error executing step: {str(e)}"

        # 3. Synthesize
        yield {"type": "status", "content": "Synthesizing final response..."}
        
        final_context = f"Original Query: {context}\n\nExecution Results:\n" + "\n".join(results_summary)
        final_context += f"\n\nInstruction: {plan.final_response_instruction}"
        
        # Use general agent for synthesis
        general_agent = self.agents_map.get("general")
        final_result = await general_agent.run(final_context)
        
        yield {
            "type": "result",
            "data": {
                "success": True,
                "response": str(final_result.output),
                "sql_data": self.sql_data,
                "step_outputs": self.step_outputs
            }
        }
