"""
FastMCP RAG Server
Exposes a tool to search for meeting context using PGVector.
"""

from fastmcp import FastMCP
from typing import Dict, Any, List
import psycopg2
from pgvector.psycopg2 import register_vector
import os
from dotenv import load_dotenv
import openai

load_dotenv()

# Initialize FastMCP
mcp = FastMCP("TLDV RAG Server")

# Initialize OpenAI client
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    schema = os.getenv("PG_SCHEMA", "public")
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
        dbname=os.getenv("PG_DB"),
        options=f"-c search_path={schema}"
    )
    # Ensure vector extension exists (or at least try, requires verify)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
    except Exception:
        conn.rollback() # Ignore if permission denied, assume it exists
        
    # Register vector type for PGVector
    register_vector(conn)
    return conn

def get_embedding(text: str) -> List[float]:
    """Generate embedding for the given text using OpenAI."""
    model = os.getenv("OPENAI_MODEL", "text-embedding-3-small")
    response = client.embeddings.create(input=text, model=model)
    return response.data[0].embedding

@mcp.tool
def search_meetings(query: str, limit: int = 5) -> Dict[str, Any]:
    """
    Search for relevant meeting context using semantic search.
    
    Args:
        query: The user's natural language query (e.g., "What was discussed about the budget?")
        limit: Number of results to return (default: 5)
    """
    try:
        # Convert query to embedding
        query_embedding = get_embedding(query)
        
        table_name = os.getenv("PG_TABLE_NAME", "meeting_embeddings")
        schema = os.getenv("PG_SCHEMA", "public")
        
        # Connect to DB and search
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # PGVector L2 distance operator is <->
                # Cosine distance is <=>
                # Using <=> for cosine distance (usually preferred for embeddings)
                sql = f"""
                    SELECT content, metadata, meeting_id, 1 - (embedding <=> %s::vector) as similarity
                    FROM {schema}.{table_name}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """
                cur.execute(sql, (query_embedding, query_embedding, limit))
                results = cur.fetchall()
                
                # Format results
                formatted_results = []
                for row in results:
                    formatted_results.append({
                        "content": row[0],
                        "metadata": row[1],
                        "meeting_id": row[2],
                        "similarity": float(row[3])
                    })
                    
        return {
            "success": True, 
            "results": formatted_results,
            "query": query
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    mcp.run()
