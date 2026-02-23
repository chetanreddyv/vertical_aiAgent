from fastmcp import FastMCP
from dotenv import load_dotenv

import os
import os.path
import base64
from email.mime.text import MIMEText
from typing import List, Optional, Dict, Any, Tuple
import sys
import os.path as _osp
sys.path.insert(0, _osp.dirname(_osp.dirname(_osp.abspath(__file__))))
from google_auth_helper import get_google_creds
from googleapiclient.discovery import build

import logging

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("gmail_server")

# Scopes are managed centrally in google_auth_helper.py

mcp = FastMCP("Gmail Server")

WATERMARK = "\n\n-By JerseySTEM Cowork Agent"


def get_gmail_service():
    """Get authenticated Gmail service using shared credentials."""
    creds = get_google_creds()
    return build("gmail", "v1", credentials=creds)


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8")


def _find_header(headers: List[Dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _extract_text_from_payload(payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (text_plain, text_html) best-effort by walking MIME parts.
    """
    text_plain = ""
    text_html = ""

    def walk(part: Dict[str, Any]):
        nonlocal text_plain, text_html

        mime_type = part.get("mimeType", "")
        body = part.get("body", {}) or {}

        data = body.get("data")
        if data:
            try:
                decoded = _b64url_decode(data).decode("utf-8", errors="replace")
            except Exception:
                decoded = ""

            if mime_type == "text/plain" and not text_plain:
                text_plain = decoded
            elif mime_type == "text/html" and not text_html:
                text_html = decoded

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload or {})
    return text_plain, text_html


def _resolve_label_id(service, label_id_or_name: str) -> str:
    """
    Accepts either a label ID (e.g., 'Label_123') or a label name (e.g., 'Work').
    Returns a label ID if found, else raises.
    """
    # Quick path: common system labels
    system = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "TRASH", "SPAM", "DRAFT"}
    if label_id_or_name in system:
        return label_id_or_name

    resp = service.users().labels().list(userId="me").execute()
    for lbl in resp.get("labels", []) or []:
        if lbl.get("id") == label_id_or_name or lbl.get("name") == label_id_or_name:
            return lbl["id"]

    raise ValueError(f"Label not found: {label_id_or_name}")


@mcp.tool()
def list_messages(max_results: int = 10, query: str = "", include_spam_trash: bool = False) -> str:
    """
    List messages. `query` uses the same search syntax as the Gmail search box (Gmail 'q'). [web:10][web:13]
    Example query: "from:someone@example.com is:unread"
    """
    logger.info(f"🛠️ Tool Called: list_messages(max_results={max_results}, query='{query}')")
    try:
        service = get_gmail_service()

        resp = service.users().messages().list(
            userId="me",
            maxResults=max_results,
            q=query or None,
            includeSpamTrash=include_spam_trash,
        ).execute()

        msgs = resp.get("messages", []) or []
        if not msgs:
            return "No messages found."

        lines = [f"Messages (showing up to {max_results}):"]
        for m in msgs:
            msg = service.users().messages().get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()

            headers = (msg.get("payload", {}) or {}).get("headers", []) or []
            subject = _find_header(headers, "Subject")
            sender = _find_header(headers, "From")
            date = _find_header(headers, "Date")
            snippet = msg.get("snippet", "")

            lines.append(f"- ID: {msg.get('id')} | {date} | {sender} | {subject}")
            if snippet:
                lines.append(f"  Snippet: {snippet}")

        logger.info(f"✅ Tool Complete: list_messages (Found {len(msgs)} messages)")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"❌ Tool Error: list_messages - {str(e)}")
        return f"Error listing messages: {str(e)}"


@mcp.tool()
def get_message(message_id: str) -> str:
    """Fetch a message and return key headers + best-effort body extraction."""
    logger.info(f"🛠️ Tool Called: get_message(message_id='{message_id}')")
    try:
        service = get_gmail_service()

        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        subject = _find_header(headers, "Subject")
        sender = _find_header(headers, "From")
        to = _find_header(headers, "To")
        date = _find_header(headers, "Date")

        text_plain, text_html = _extract_text_from_payload(payload)

        out = []
        out.append(f"Message ID: {msg.get('id')}")
        out.append(f"Thread ID: {msg.get('threadId')}")
        out.append(f"Date: {date}")
        out.append(f"From: {sender}")
        out.append(f"To: {to}")
        out.append(f"Subject: {subject}")
        out.append("\n--- BODY (text/plain) ---")
        out.append(text_plain.strip() if text_plain else "[No text/plain body found]")
        out.append("\n--- BODY (text/html) ---")
        out.append(text_html.strip() if text_html else "[No text/html body found]")

        logger.info(f"✅ Tool Complete: get_message")
        return "\n".join(out)
    except Exception as e:
        logger.error(f"❌ Tool Error: get_message - {str(e)}")
        return f"Error getting message: {str(e)}"


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    mime_subtype: str = "plain",  # "plain" or "html"
) -> str:
    """
    Send an email via Gmail API by base64url-encoding an RFC 2822 message in `raw`. [web:1]
    """
    logger.info(f"🛠️ Tool Called: send_email(to='{to}', subject='{subject}')")
    try:
        service = get_gmail_service()

        msg = MIMEText(f"{body}{WATERMARK}", _subtype=mime_subtype, _charset="utf-8")
        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc
        if bcc:
            msg["bcc"] = bcc

        raw = _b64url_encode(msg.as_bytes())
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()

        logger.info(f"✅ Tool Complete: send_email (ID: {sent.get('id')})")
        return f"Email sent. Message ID: {sent.get('id')}"
    except Exception as e:
        logger.error(f"❌ Tool Error: send_email - {str(e)}")
        return f"Error sending email: {str(e)}"


@mcp.tool()
def mark_read(message_id: str) -> str:
    """Mark a message as read by removing the UNREAD label."""
    try:
        service = get_gmail_service()
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return f"Marked read: {message_id}"
    except Exception as e:
        return f"Error marking read: {str(e)}"


@mcp.tool()
def mark_unread(message_id: str) -> str:
    """Mark a message as unread by adding the UNREAD label."""
    try:
        service = get_gmail_service()
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": ["UNREAD"]},
        ).execute()
        return f"Marked unread: {message_id}"
    except Exception as e:
        return f"Error marking unread: {str(e)}"


@mcp.tool()
def trash_message(message_id: str) -> str:
    """Move a message to Trash."""
    try:
        service = get_gmail_service()
        service.users().messages().trash(userId="me", id=message_id).execute()
        return f"Trashed message: {message_id}"
    except Exception as e:
        return f"Error trashing message: {str(e)}"


@mcp.tool()
def list_labels() -> str:
    """List Gmail labels."""
    try:
        service = get_gmail_service()
        resp = service.users().labels().list(userId="me").execute()
        labels = resp.get("labels", []) or []
        if not labels:
            return "No labels found."

        lines = ["Labels:"]
        for lbl in labels:
            lines.append(f"- {lbl.get('name')} (ID: {lbl.get('id')})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing labels: {str(e)}"


@mcp.tool()
def add_label(message_id: str, label: str) -> str:
    """Add a label to a message (label can be a label ID or label name)."""
    try:
        service = get_gmail_service()
        label_id = _resolve_label_id(service, label)

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

        return f"Added label '{label}' to message: {message_id}"
    except Exception as e:
        return f"Error adding label: {str(e)}"


@mcp.tool()
def remove_label(message_id: str, label: str) -> str:
    """Remove a label from a message (label can be a label ID or label name)."""
    try:
        service = get_gmail_service()
        label_id = _resolve_label_id(service, label)

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": [label_id]},
        ).execute()

        return f"Removed label '{label}' from message: {message_id}"
    except Exception as e:
        return f"Error removing label: {str(e)}"


if __name__ == "__main__":
    mcp.run()
