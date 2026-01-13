from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def get_temporal_context():
    """Get current date and time context"""
    return f"Current Date and Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

def format_schema_rows(schema_rows, include_dbs=None, lightweight=False):
    """
    Format database schema rows into a readable string.
    
    Args:
        schema_rows: List of dictionaries containing schema info
        include_dbs: Optional list of database names to include. If None, excludes system DBs.
        lightweight: If True, uses a more compact format to save tokens.
    """
    schema_map = {}
    for row in schema_rows:
        db = row.get('TABLE_SCHEMA') or 'defaultdb'
        tbl = row.get('TABLE_NAME')
        col = row.get('COLUMN_NAME')
        typ = row.get('DATA_TYPE')
        nullable = row.get('IS_NULLABLE', 'UNKNOWN')
        
        # Apply filtering
        if include_dbs is not None:
            if db not in include_dbs:
                continue
        else:
            excluded_dbs = {'information_schema', 'mysql', 'performance_schema', 'sys'}
            if db in excluded_dbs:
                continue

        if db not in schema_map:
            schema_map[db] = {}
        if tbl not in schema_map[db]:
            schema_map[db][tbl] = []
        schema_map[db][tbl].append({
            'name': col,
            'type': typ,
            'nullable': nullable
        })
    
    if not schema_map:
        return "No schema available for the selected databases."

    schema_text = "=" * 60 + "\nDATABASE SCHEMA OVERVIEW\n" + "=" * 60 + "\n\n"
    
    for db_name, tables in schema_map.items():
        schema_text += f"📊 Database: {db_name}\n" + "-"*60 + "\n"
        for table_name, columns in tables.items():
            if lightweight:
                # Compact format: Table: Name (col1, col2, col3)
                cols_str = ", ".join(f"{c['name']}" for c in columns)
                schema_text += f"  Table: {table_name} ({len(columns)} cols): {cols_str}\n"
            else:
                schema_text += f"\n  Table: {table_name}\n  Columns ({len(columns)} total):\n"
                for col in columns:
                    nullable_text = "✓" if col['nullable'] == 'YES' else "✗"
                    schema_text += f"    • {col['name']} ({col['type']}) - Nullable: {nullable_text}\n"
            schema_text += "\n"
        schema_text += "\n"
    
    logger.info(f"Schema formatting completed (Dbs: {list(schema_map.keys())}, Lightweight: {lightweight})")
    return schema_text

def format_query_results(result_data):
    """Format SQL query results into a readable table."""
    if not result_data.get('success'):
        return f"❌ Query Error: {result_data.get('error', 'Unknown error')}"
    
    rows = result_data.get('rows', [])
    if not rows:
        return "✅ Query executed successfully. No rows returned."
    
    # Get column names from first row
    columns = list(rows[0].keys())
    
    # Calculate column widths
    col_widths = {}
    for col in columns:
        col_widths[col] = max(
            len(str(col)),
            max(len(str(row.get(col, ''))) for row in rows)
        )
    
    # Build table header
    header = " | ".join(str(col).ljust(col_widths[col]) for col in columns)
    separator = "-+-".join("-" * col_widths[col] for col in columns)
    
    # Build table rows
    table_rows = []
    for row in rows:
        table_rows.append(" | ".join(
            str(row.get(col, '')).ljust(col_widths[col]) for col in columns
        ))
    
    # Combine everything
    output = f"\n{header}\n{separator}\n"
    output += "\n".join(table_rows)
    output += f"\n\n📊 Total rows: {len(rows)}"
    
    return output

import os
import json
from typing import List, Dict, Any

def check_grounding(response_text: str, retrieved_context: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Evaluates if the response is supported by the retrieved context.
    Uses a lightweight LLM call to verify claims.
    """
    try:
        import openai
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        context_str = "\n\n".join([
            f"Chunk {i+1}: {c.get('content', '')} (Source: {c.get('citation_label', 'Unknown')})"
            for i, c in enumerate(retrieved_context)
        ])
        
        prompt = f"""
        You are a hallucination detector. 
        Verify if the following Response is fully supported by the Context.
        
        Context:
        {context_str[:15000]} -- Truncated if too long
        
        Response:
        {response_text}
        
        Task:
        1. Identify key claims in the response.
        2. Check if each claim is supported by the context.
        3. Assign a grounding score (0.0 - 1.0).
        
        Return JSON: {{ "score": float, "analysis": "string explanation", "supported": boolean }}
        """
        
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        logger.error(f"Grounding check failed: {e}")
        return {"score": 0.0, "analysis": f"Check failed: {e}", "supported": False}
