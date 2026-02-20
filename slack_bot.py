"""
slack_bot.py — Slack Bolt (Socket Mode) bot for the Vertical AI Agent system.

This process listens for Slack messages and routes them through the existing
FastAPI backend (/query-stream). Run alongside the FastAPI server:

    .venv/bin/python slack_bot.py

Required environment variables (in .env):
    SLACK_BOT_TOKEN      — Bot User OAuth Token (xoxb-...)
    SLACK_APP_TOKEN      — App-Level Token with connections:write scope (xapp-...)
    SLACK_SIGNING_SECRET — From Slack App Basic Information page
    BASIC_AUTH_USERNAME  — Same as FastAPI basic auth
    BASIC_AUTH_PASSWORD  — Same as FastAPI basic auth
"""

import os
import json
import logging
import asyncio
import aiohttp
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

API_BASE_URL = os.getenv("AI_AGENT_API_URL", "http://localhost:8001")
API_USERNAME = os.getenv("BASIC_AUTH_USERNAME", "admin")
API_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "internalsecret")

# ── Slack App ─────────────────────────────────────────────────────────────────
app = AsyncApp(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def call_agent_stream(query: str, session_id: str = "default") -> list:
    """
    Call the FastAPI /query-stream endpoint and yield all events.
    """
    url = f"{API_BASE_URL}/query-stream"
    auth = aiohttp.BasicAuth(API_USERNAME, API_PASSWORD)
    params = {"query": query, "session_id": session_id}

    events = []

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, auth=auth, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if not payload_str:
                    continue
                try:
                    event = json.loads(payload_str)
                    events.append(event)
                    # If it's a result or confirmation_request, we can stop listening if we want,
                    # but let's collect everything for simplicity in this helper.
                except json.JSONDecodeError:
                    continue

    return events


async def call_confirm_step(session_id: str, step_id: str, instruction: str) -> list:
    """
    Call the FastAPI /confirm-step endpoint to resume execution.
    """
    url = f"{API_BASE_URL}/confirm-step"
    auth = aiohttp.BasicAuth(API_USERNAME, API_PASSWORD)
    payload = {
        "session_id": session_id,
        "step_id": step_id,
        "approved_instruction": instruction
    }

    events = []
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, auth=auth, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if not payload_str:
                    continue
                try:
                    event = json.loads(payload_str)
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    return events


import re

def format_response_for_slack(response_text: str) -> str:
    """
    Convert standard Markdown to Slack's mrkdwn format.
    """
    # 1. Convert Bold: **text** -> *text*
    response_text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', response_text)
    
    # 2. Convert Headers: ### Header -> *Header*
    # Slack doesn't support # headers, so we make them bold lines
    response_text = re.sub(r'^#+\s+(.*?)$', r'*\1*', response_text, flags=re.MULTILINE)
    
    # 3. Convert standard Markdown Links: [link text](url) -> <url|link text>
    response_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<\2|\1>', response_text)

    # 4. Handle very long responses
    if len(response_text) > 3800:
        response_text = response_text[:3800] + "\n\n_...response truncated_"
        
    return response_text


async def post_thinking(say, channel: str, thread_ts: str | None) -> str:
    """Post a 'thinking...' placeholder and return its ts for later update."""
    kwargs = {"channel": channel, "text": "🤔 Thinking..."}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    result = await app.client.chat_postMessage(**kwargs)
    return result["ts"]


async def update_message(channel: str, ts: str, text: str, blocks: list = None):
    """Update an existing Slack message in-place."""
    kwargs = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    await app.client.chat_update(**kwargs)


def create_confirmation_blocks(step_id: str, instruction: str, session_id: str):
    """Create Slack blocks for HITL confirmation."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Action Required*: The agent is requesting permission to perform the following task:\n\n> {instruction}"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_step",
                    "value": json.dumps({
                        "step_id": step_id,
                        "instruction": instruction,
                        "session_id": session_id
                    })
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                    "action_id": "cancel_step"
                }
            ]
        }
    ]


# ── Event Handlers ────────────────────────────────────────────────────────────

async def handle_query_events(events: list, channel: str, thinking_ts: str, thread_ts: str):
    """Process a list of events from the backend and update Slack."""
    final_response = "No response received."
    
    for event in events:
        etype = event.get("type")
        if etype == "confirmation_request":
            # PAUSE: Show buttons
            instruction = event.get("instruction", "Confirm action")
            step_id = event.get("step_id")
            session_id = event.get("session_id", thread_ts)
            
            blocks = create_confirmation_blocks(step_id, instruction, session_id)
            await update_message(channel, thinking_ts, "Action confirmation required:", blocks=blocks)
            return # Stop processing until user clicks button
            
        elif etype == "result":
            data = event.get("data", {})
            final_response = data.get("response", final_response)
        elif etype == "error":
            final_response = f"⚠️ Error: {event.get('error', 'Unknown error')}"

    await update_message(channel, thinking_ts, format_response_for_slack(final_response))


@app.event("app_mention")
async def handle_mention(event, say, logger):
    """Handle @bot mentions in channels."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    bot_user_id = (await app.client.auth_test())["user_id"]

    # Strip the bot mention from the text
    raw_text = event.get("text", "")
    query = raw_text.replace(f"<@{bot_user_id}>", "").strip()

    if not query:
        await say(
            text="Hi! Ask me anything — I can query your database, check emails, search meetings, manage Jira, and more.",
            thread_ts=thread_ts,
        )
        return

    logger.info(f"📨 Mention from {event.get('user')} in {channel}: {query[:80]}")
    thinking_ts = await post_thinking(say, channel, thread_ts)

    try:
        events = await call_agent_stream(query, session_id=thread_ts)
        await handle_query_events(events, channel, thinking_ts, thread_ts)
    except Exception as e:
        logger.error(f"Error calling agent: {e}", exc_info=True)
        await update_message(channel, thinking_ts, f"⚠️ Something went wrong: {str(e)}")


@app.event("message")
async def handle_dm(event, say, logger):
    """Handle direct messages to the bot."""
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    query = event.get("text", "").strip()

    if not query: return
    logger.info(f"📨 DM from {event.get('user')}: {query[:80]}")
    thinking_ts = await post_thinking(say, channel, thread_ts=None)

    try:
        events = await call_agent_stream(query, session_id=thread_ts)
        await handle_query_events(events, channel, thinking_ts, thread_ts)
    except Exception as e:
        logger.error(f"Error calling agent: {e}", exc_info=True)
        await update_message(channel, thinking_ts, f"⚠️ Something went wrong: {str(e)}")


@app.action("approve_step")
async def handle_approve(ack, body, logger):
    """Handle the 'Approve' button click."""
    await ack()
    
    value = json.loads(body["actions"][0]["value"])
    step_id = value["step_id"]
    instruction = value["instruction"]
    session_id = value["session_id"]
    
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    
    # Update message to show processing
    await update_message(channel, message_ts, "✅ *Approved*. Resuming execution...")
    
    try:
        events = await call_confirm_step(session_id, step_id, instruction)
        # Handle the remaining events in the same thread
        await handle_query_events(events, channel, message_ts, session_id)
    except Exception as e:
        logger.error(f"Error resuming agent: {e}", exc_info=True)
        await update_message(channel, message_ts, f"⚠️ Error resuming execution: {str(e)}")


@app.action("cancel_step")
async def handle_cancel(ack, body, logger):
    """Handle the 'Cancel' button click."""
    await ack()
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    await update_message(channel, message_ts, "❌ *Action cancelled by user*.")


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main():
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN.startswith("xoxb-your"):
        logger.error("❌ SLACK_BOT_TOKEN is not set.")
        return
    if not SLACK_APP_TOKEN or SLACK_APP_TOKEN.startswith("xapp-your"):
        logger.error("❌ SLACK_APP_TOKEN is not set.")
        return

    logger.info("⚡️ Starting Slack bot in Socket Mode...")
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
