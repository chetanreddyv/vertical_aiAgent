from fastmcp import FastMCP, Context
from jira import JIRA
from jira.exceptions import JIRAError
import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize FastMCP server
mcp = FastMCP(
    name="Jira MCP Server"
)

# Global Jira client
_jira_client = None

WATERMARK = "\n\n-By JerseySTEM Cowork Agent"

def get_jira_client() -> JIRA:
    """Initialize and return Jira client"""
    global _jira_client
    if _jira_client is None:
        jira_url = os.getenv("JIRA_URL")
        jira_pat = os.getenv("JIRA_PAT")
        
        if jira_pat:
            # Bearer Token (PAT) Authentication for Jira Server/Data Center
            _jira_client = JIRA(
                server=jira_url,
                token_auth=jira_pat
            )
        else:
            # Basic Authentication fallback (Cloud)
            _jira_client = JIRA(
                server=jira_url,
                basic_auth=(
                    os.getenv("JIRA_USERNAME"),
                    os.getenv("JIRA_API_TOKEN")
                )
            )
    return _jira_client

# ==================== SEARCH & RETRIEVAL ====================

@mcp.tool()
async def jira_search(
    jql_query: str,
    max_results: int = 50,
    fields: str = "summary,status,assignee,priority,issuetype,created,updated",
    ctx: Context = None
) -> List[Dict[str, Any]]:
    """
    Search Jira issues using JQL (Jira Query Language).
    
    Args:
        jql_query: JQL query string (e.g., "project = PROJ AND status = Open")
        max_results: Maximum number of results to return
        fields: Comma-separated field names to retrieve
    
    Returns:
        List of issues matching the query
    """
    if ctx:
        await ctx.info(f"Searching with JQL: {jql_query}")
    
    def search():
        jira = get_jira_client()
        issues = jira.search_issues(
            jql_query,
            maxResults=max_results,
            fields=fields
        )
        
        results = []
        for issue in issues:
            issue_data = {
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "type": getattr(issue.fields.issuetype, 'name', 'Unknown') if getattr(issue.fields, 'issuetype', None) else 'Unknown',
                "priority": issue.fields.priority.name if issue.fields.priority else None,
                "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned",
                "created": str(issue.fields.created),
                "updated": str(issue.fields.updated),
                "url": f"{os.getenv('JIRA_URL')}/browse/{issue.key}"
            }
            results.append(issue_data)
        
        return results
    
    try:
        return await asyncio.to_thread(search)
    except JIRAError as e:
        return {"error": f"Jira error: {str(e)}"}

@mcp.tool()
async def jira_get_issue(
    issue_key: str,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Get detailed information about a specific Jira issue.
    
    Args:
        issue_key: The issue key (e.g., "PROJ-123")
    
    Returns:
        Detailed issue information
    """
    if ctx:
        await ctx.info(f"Fetching issue: {issue_key}")
    
    def get_issue():
        jira = get_jira_client()
        issue = jira.issue(issue_key)
        
        return {
            "key": issue.key,
            "summary": issue.fields.summary,
            "description": issue.fields.description or "",
            "status": issue.fields.status.name,
            "type": getattr(issue.fields.issuetype, 'name', 'Unknown') if getattr(issue.fields, 'issuetype', None) else 'Unknown',
            "priority": issue.fields.priority.name if issue.fields.priority else None,
            "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned",
            "reporter": issue.fields.reporter.displayName if issue.fields.reporter else None,
            "created": str(issue.fields.created),
            "updated": str(issue.fields.updated),
            "resolution": issue.fields.resolution.name if issue.fields.resolution else None,
            "labels": issue.fields.labels,
            "components": [c.name for c in issue.fields.components],
            "url": f"{os.getenv('JIRA_URL')}/browse/{issue.key}"
        }
    
    try:
        return await asyncio.to_thread(get_issue)
    except JIRAError as e:
        return {"error": f"Issue not found or access denied: {str(e)}"}

# ==================== CREATE & UPDATE ====================

@mcp.tool()
async def jira_create_issue(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Task",
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a new Jira issue.
    
    Args:
        project_key: Project key (e.g., "PROJ")
        summary: Issue summary/title
        description: Issue description
        issue_type: Issue type (Task, Bug, Story, etc.)
        priority: Priority level (optional)
        assignee: Username to assign (optional)
        labels: List of labels (optional)
    
    Returns:
        Created issue details
    """
    if ctx:
        await ctx.info(f"Creating {issue_type} in project {project_key}")
    
    def create():
        jira = get_jira_client()
        
        issue_dict = {
            "project": {"key": project_key},
            "summary": summary,
            "description": f"{description}{WATERMARK}",
            "issuetype": {"name": issue_type}
        }
        
        if priority:
            issue_dict["priority"] = {"name": priority}
        
        if assignee:
            issue_dict["assignee"] = {"name": assignee}
        
        if labels:
            issue_dict["labels"] = labels
        
        new_issue = jira.create_issue(fields=issue_dict)
        
        return {
            "key": new_issue.key,
            "url": f"{os.getenv('JIRA_URL')}/browse/{new_issue.key}",
            "message": f"Successfully created {new_issue.key}"
        }
    
    try:
        return await asyncio.to_thread(create)
    except JIRAError as e:
        return {"error": f"Failed to create issue: {str(e)}"}

@mcp.tool()
async def jira_update_issue(
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Update an existing Jira issue.
    
    Args:
        issue_key: The issue key to update
        summary: New summary (optional)
        description: New description (optional)
        priority: New priority (optional)
        assignee: New assignee username (optional)
        labels: New labels list (optional)
    
    Returns:
        Update confirmation
    """
    if ctx:
        await ctx.info(f"Updating issue: {issue_key}")
    
    def update():
        jira = get_jira_client()
        issue = jira.issue(issue_key)
        
        update_fields = {}
        if summary is not None:
            update_fields["summary"] = summary
        if description is not None:
            update_fields["description"] = f"{description}{WATERMARK}"
        if priority is not None:
            update_fields["priority"] = {"name": priority}
        if assignee is not None:
            update_fields["assignee"] = {"name": assignee}
        if labels is not None:
            update_fields["labels"] = labels
        
        issue.update(fields=update_fields)
        
        return {
            "key": issue_key,
            "message": f"Successfully updated {issue_key}",
            "url": f"{os.getenv('JIRA_URL')}/browse/{issue_key}"
        }
    
    try:
        return await asyncio.to_thread(update)
    except JIRAError as e:
        return {"error": f"Failed to update issue: {str(e)}"}

# ==================== COMMENTS ====================

@mcp.tool()
async def jira_add_comment(
    issue_key: str,
    comment_body: str,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Add a comment to a Jira issue.
    
    Args:
        issue_key: The issue key
        comment_body: Comment text
    
    Returns:
        Comment confirmation
    """
    if ctx:
        await ctx.info(f"Adding comment to {issue_key}")
    
    def add_comment():
        jira = get_jira_client()
        comment = jira.add_comment(issue_key, f"{comment_body}{WATERMARK}")
        
        return {
            "issue_key": issue_key,
            "comment_id": comment.id,
            "message": f"Comment added to {issue_key}"
        }
    
    try:
        return await asyncio.to_thread(add_comment)
    except JIRAError as e:
        return {"error": f"Failed to add comment: {str(e)}"}

@mcp.tool()
async def jira_get_comments(
    issue_key: str,
    ctx: Context = None
) -> List[Dict[str, Any]]:
    """
    Get all comments from a Jira issue.
    
    Args:
        issue_key: The issue key
    
    Returns:
        List of comments
    """
    if ctx:
        await ctx.info(f"Fetching comments for {issue_key}")
    
    def get_comments():
        jira = get_jira_client()
        issue = jira.issue(issue_key)
        comments = jira.comments(issue)
        
        return [
            {
                "id": comment.id,
                "author": comment.author.displayName,
                "body": comment.body,
                "created": str(comment.created),
                "updated": str(comment.updated)
            }
            for comment in comments
        ]
    
    try:
        return await asyncio.to_thread(get_comments)
    except JIRAError as e:
        return {"error": f"Failed to get comments: {str(e)}"}

# ==================== TRANSITIONS & WORKFLOW ====================

@mcp.tool()
async def jira_transition_issue(
    issue_key: str,
    transition_name: str,
    comment: Optional[str] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Transition an issue to a new status (e.g., In Progress, Done).
    
    Args:
        issue_key: The issue key
        transition_name: Name of the transition (e.g., "In Progress", "Done")
        comment: Optional comment to add with the transition
    
    Returns:
        Transition confirmation
    """
    if ctx:
        await ctx.info(f"Transitioning {issue_key} to {transition_name}")
    
    def transition():
        jira = get_jira_client()
        issue = jira.issue(issue_key)
        
        # Get available transitions
        transitions = jira.transitions(issue)
        transition_id = None
        
        for t in transitions:
            if t['name'].lower() == transition_name.lower():
                transition_id = t['id']
                break
        
        if not transition_id:
            available = [t['name'] for t in transitions]
            return {
                "error": f"Transition '{transition_name}' not found",
                "available_transitions": available
            }
        
        # Perform transition
        if comment:
            comment = f"{comment}{WATERMARK}"
        jira.transition_issue(issue, transition_id, comment=comment)
        
        return {
            "issue_key": issue_key,
            "new_status": transition_name,
            "message": f"Successfully transitioned {issue_key} to {transition_name}"
        }
    
    try:
        return await asyncio.to_thread(transition)
    except JIRAError as e:
        return {"error": f"Failed to transition issue: {str(e)}"}

@mcp.tool()
async def jira_get_transitions(
    issue_key: str,
    ctx: Context = None
) -> List[Dict[str, str]]:
    """
    Get available workflow transitions for an issue.
    
    Args:
        issue_key: The issue key
    
    Returns:
        List of available transitions
    """
    if ctx:
        await ctx.info(f"Fetching transitions for {issue_key}")
    
    def get_transitions():
        jira = get_jira_client()
        issue = jira.issue(issue_key)
        transitions = jira.transitions(issue)
        
        return [
            {
                "id": t['id'],
                "name": t['name'],
                "to_status": t['to']['name']
            }
            for t in transitions
        ]
    
    try:
        return await asyncio.to_thread(get_transitions)
    except JIRAError as e:
        return {"error": f"Failed to get transitions: {str(e)}"}

# ==================== PROJECT MANAGEMENT ====================

@mcp.tool()
async def jira_list_projects(ctx: Context = None) -> List[Dict[str, str]]:
    """
    List all accessible Jira projects.
    
    Returns:
        List of projects with keys and names
    """
    if ctx:
        await ctx.info("Fetching all projects")
    
    def list_projects():
        jira = get_jira_client()
        projects = jira.projects()
        
        return [
            {
                "key": p.key,
                "name": p.name,
                "type": getattr(p, 'projectTypeKey', 'unknown')
            }
            for p in projects
        ]
    
    try:
        return await asyncio.to_thread(list_projects)
    except JIRAError as e:
        return {"error": f"Failed to list projects: {str(e)}"}

@mcp.tool()
async def jira_get_user_issues(
    username: Optional[str] = None,
    status: Optional[str] = None,
    max_results: int = 50,
    ctx: Context = None
) -> List[Dict[str, Any]]:
    """
    Get issues assigned to a specific user or current user.
    
    Args:
        username: Jira username (defaults to current user if not provided)
        status: Filter by status (optional)
        max_results: Maximum number of results
    
    Returns:
        List of assigned issues
    """
    if ctx:
        await ctx.info(f"Fetching issues for user: {username or 'current user'}")
    
    def get_issues():
        jira = get_jira_client()
        
        # Build JQL query
        if username:
            jql = f"assignee = {username}"
        else:
            jql = "assignee = currentUser()"
        
        if status:
            jql += f" AND status = '{status}'"
        
        jql += " ORDER BY updated DESC"
        
        issues = jira.search_issues(jql, maxResults=max_results)
        
        return [
            {
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "priority": issue.fields.priority.name if issue.fields.priority else None,
                "updated": str(issue.fields.updated),
                "url": f"{os.getenv('JIRA_URL')}/browse/{issue.key}"
            }
            for issue in issues
        ]
    
    try:
        return await asyncio.to_thread(get_issues)
    except JIRAError as e:
        return {"error": f"Failed to get user issues: {str(e)}"}

# ==================== RESOURCES ====================

@mcp.resource("jira://config")
async def jira_config() -> str:
    """Provides Jira server configuration"""
    return f"""Jira Server Configuration:
- Server URL: {os.getenv('JIRA_URL')}
- Username: {os.getenv('JIRA_USERNAME')}
- Connected: {_jira_client is not None}
"""

@mcp.resource("jira://project/{project_key}")
async def jira_project_info(project_key: str) -> str:
    """Get project information by project key"""
    def get_project():
        jira = get_jira_client()
        project = jira.project(project_key)
        return f"""Project: {project.name} ({project.key})
Lead: {project.lead.displayName if hasattr(project, 'lead') else 'N/A'}
Description: {getattr(project, 'description', 'No description')}
URL: {os.getenv('JIRA_URL')}/browse/{project.key}
"""
    
    try:
        return await asyncio.to_thread(get_project)
    except JIRAError as e:
        return f"Error: {str(e)}"

# ==================== RUN SERVER ====================

if __name__ == "__main__":
    mcp.run()
