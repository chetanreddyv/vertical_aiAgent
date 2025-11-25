"""
FastMCP MySQL Server
Exposes database tools for schema inspection, query execution, and natural-language SQL generation.
"""

from fastmcp import FastMCP
from typing import Dict, Any, Optional
import pymysql
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()
mcp = FastMCP("MySQL Database Server")

def get_connection(database: Optional[str] = None):
    conn_params = {
        'host': os.getenv('DB_HOST', 'localhost'),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', ''),
        'port': int(os.getenv('DB_PORT', '3306')),
        'cursorclass': pymysql.cursors.DictCursor
    }
    if database:
        conn_params['database'] = database
    return pymysql.connect(**conn_params)

@mcp.tool
def test_connection() -> Dict[str, Any]:
    """Test connection to MySQL server."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        return {'success': True, 'message': 'Connection successful.'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@mcp.tool
def list_databases() -> Dict[str, Any]:
    """List all databases."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW DATABASES")
                dbs = [row["Database"] for row in cursor.fetchall()]
        return {'success': True, 'databases': dbs}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@mcp.tool
def list_tables(database: str) -> Dict[str, Any]:
    """List tables in a selected database."""
    try:
        with get_connection(database) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                tables = [list(row.values())[0] for row in cursor.fetchall()]
        return {'success': True, 'tables': tables, 'database': database}
    except Exception as e:
        return {'success': False, 'error': str(e), 'database': database}

@mcp.tool
def table_schema(database: str, table: str) -> Dict[str, Any]:
    """Show table schema: columns and types."""
    try:
        with get_connection(database) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                columns = cursor.fetchall()
        return {'success': True, 'database': database, 'table': table, 'columns': columns}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@mcp.tool
def schema_info(database: Optional[str] = None) -> Dict[str, Any]:
    """Full schema info: tables/columns/types (using information_schema)."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                if database:
                    cursor.execute(
                        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s",
                        (database,)
                    )
                else:
                    cursor.execute(
                        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS"
                    )
                info = cursor.fetchall()
        return {'success': True, 'schema': info}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@mcp.tool
def sample_rows(database: str, table: str, limit: int = 5) -> Dict[str, Any]:
    """Get sample data from a table, up to `limit` rows (default 5)."""
    try:
        with get_connection(database) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT * FROM `{table}` LIMIT %s", (limit,))
                rows = cursor.fetchall()
        return {'success': True, 'database': database, 'table': table, 'rows': rows}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@mcp.tool
def execute_query(query: str, database: Optional[str] = None, read_only: bool = True) -> Dict[str, Any]:
    """
    Execute SQL query in a database. Restrict to read-only by default.
    """
    try:
        allowed = ('SELECT', 'SHOW', 'DESCRIBE', 'DESC', 'EXPLAIN')
        if read_only and not query.strip().upper().startswith(allowed):
            return {'success': False, 'error': 'Only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed in read-only mode.'}

        with get_connection(database) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                if cursor.description:
                    rows = cursor.fetchall()
                    return {'success': True, 'query': query, 'rows': rows}
                else:
                    return {'success': True, 'query': query, 'affected': cursor.rowcount}
    except Exception as e:
        return {'success': False, 'error': str(e), 'query': query}

# ===== Server entry point =====
if __name__ == "__main__":
    print("Starting FastMCP MySQL Server ...")
    mcp.run()
