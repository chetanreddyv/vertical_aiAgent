"""
FastMCP RAG Server
Exposes a tool to search for meeting context using PGVector.
"""

from fastmcp import FastMCP
from typing import Dict, Any, List, Optional
import psycopg2
from pgvector.psycopg2 import register_vector
import os
from dotenv import load_dotenv
import google.generativeai as genai
from datetime import datetime, timedelta
import json

load_dotenv()

# Initialize FastMCP
mcp = FastMCP("TLDV RAG Server")

# Initialize Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    schema = os.getenv("PG_SCHEMA", "public")
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5434"),
        user=os.getenv("PG_USER", "chetan"),
        password=os.getenv("PG_PASSWORD", ""),
        dbname=os.getenv("PG_DB", "vectordb"),
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
    """Generate embedding for the given text using Gemini."""
    model = "models/text-embedding-004"
    result = genai.embed_content(
        model=model,
        content=text,
        task_type="retrieval_document"
    )
    return result['embedding']

@mcp.tool
def search_meetings(
    query: str, 
    limit: int = 20,
    min_similarity: float = 0.2,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speaker: Optional[str] = None,
    meeting_id: Optional[str] = None,
    deduplicate: bool = True,
    max_results_per_meeting: int = 3
) -> Dict[str, Any]:
    """
    Search for relevant meeting context using advanced hybrid semantic + keyword search.
    """
    return search_meetings_logic(
        query=query,
        limit=limit,
        min_similarity=min_similarity,
        start_date=start_date,
        end_date=end_date,
        speaker=speaker,
        meeting_id=meeting_id,
        deduplicate=deduplicate,
        max_results_per_meeting=max_results_per_meeting
    )

def search_meetings_logic(
    query: str, 
    limit: int = 20,
    min_similarity: float = 0.2,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speaker: Optional[str] = None,
    meeting_id: Optional[str] = None,
    deduplicate: bool = True,
    max_results_per_meeting: int = 3,
    include_context: bool = False
) -> Dict[str, Any]:
    """
    Core logic for search_meetings, separated for testing.
    Uses Reciprocal Rank Fusion (RRF) and Window Functions for context.
    """
    try:
        # Convert query to embedding
        query_embedding = get_embedding(query)
        
        table_name = os.getenv("PG_TABLE_NAME", "meeting_embeddings")
        schema = os.getenv("PG_SCHEMA", "public")
        
        # Build WHERE clauses for both semantic and keyword candidates
        where_clauses = []
        where_params = []
        
        # Date filtering
        if start_date:
            where_clauses.append("((metadata->>'date')::timestamp >= %s::timestamp OR metadata->>'date' IS NULL)")
            where_params.append(start_date)
        if end_date:
            where_clauses.append("((metadata->>'date')::timestamp <= %s::timestamp OR metadata->>'date' IS NULL)")
            where_params.append(end_date)
            
        # Speaker filtering
        if speaker:
            where_clauses.append("LOWER(metadata->>'speaker') LIKE LOWER(%s)")
            where_params.append(f"%{speaker}%")
            
        # Meeting ID filtering
        if meeting_id:
            where_clauses.append("meeting_id = %s")
            where_params.append(meeting_id)
        
        base_where = ""
        if where_clauses:
            base_where = "AND " + " AND ".join(where_clauses)
            
        # Connect to DB
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # 1. RRF Implementation using CTEs
                # We fetch top 100 candidates from both methods to fuse.
                # Note: We apply min_similarity to the Semantic Stream strictly.
                
                # Context Window Logic:
                # If include_context is True, we use window functions to get prev/next.
                # However, doing this on the whole table is expensive.
                # We will fetch the top hits first, then join back to get context.
                # BUT the user requirement is "single SQL query". 
                # We can do this by selecting the context in the final projection using subqueries/lateral joins.
                
                sql = f"""
                WITH semantic_candidates AS (
                    SELECT 
                        id, 
                        1 - (embedding <=> %s::vector) as similarity
                    FROM {schema}.{table_name}
                    WHERE 1 - (embedding <=> %s::vector) >= %s
                    {base_where}
                    ORDER BY similarity DESC
                    LIMIT 100
                ),
                keyword_candidates AS (
                    SELECT 
                        id, 
                        ts_rank_cd(to_tsvector('english', content), plainto_tsquery('english', %s)) as keyword_rank
                    FROM {schema}.{table_name}
                    WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                    {base_where}
                    ORDER BY keyword_rank DESC
                    LIMIT 100
                ),
                fused_scores AS (
                    SELECT 
                        COALESCE(s.id, k.id) as id,
                        s.similarity,
                        k.keyword_rank,
                        COALESCE(1.0 / (60 + ROW_NUMBER() OVER (ORDER BY s.similarity DESC)), 0.0) as rrf_sem,
                        COALESCE(1.0 / (60 + ROW_NUMBER() OVER (ORDER BY k.keyword_rank DESC)), 0.0) as rrf_kw
                    FROM semantic_candidates s
                    FULL OUTER JOIN keyword_candidates k ON s.id = k.id
                ),
                final_hits AS (
                    SELECT 
                        f.id,
                        f.similarity,
                        f.keyword_rank,
                        (f.rrf_sem + f.rrf_kw) as relevance_score
                    FROM fused_scores f
                    ORDER BY relevance_score DESC
                    LIMIT %s
                )
                SELECT 
                    m.content, 
                    m.metadata, 
                    m.meeting_id, 
                    h.similarity, 
                    h.keyword_rank,
                    h.relevance_score,
                    -- Context retrieval (Time-based adjacency)
                    CASE WHEN %s THEN (
                        SELECT content 
                        FROM {schema}.{table_name} prev
                        WHERE prev.meeting_id = m.meeting_id 
                          AND (prev.metadata->>'starttime')::int < (m.metadata->>'starttime')::int
                        ORDER BY (prev.metadata->>'starttime')::int DESC 
                        LIMIT 1
                    ) ELSE NULL END as prev_chunk,
                    CASE WHEN %s THEN (
                        SELECT content 
                        FROM {schema}.{table_name} next
                        WHERE next.meeting_id = m.meeting_id 
                          AND (next.metadata->>'starttime')::int > (m.metadata->>'starttime')::int
                        ORDER BY (next.metadata->>'starttime')::int ASC 
                        LIMIT 1
                    ) ELSE NULL END as next_chunk
                FROM final_hits h
                JOIN {schema}.{table_name} m ON h.id = m.id
                """
                
                # Parameters:
                # 1. Semantic: embedding, embedding, min_similarity
                # 2. Keyword: query, query
                # 3. Limit: limit * multiplier (for dedup)
                # 4. Context switches: include_context, include_context
                
                dedup_limit = limit * 3 if deduplicate else limit
                
                # Base params for WHERE clauses need to be repeated for both CTEs
                # Order: 
                # Sem: emb, emb, min_sim, [where_params]
                # Key: query, query, [where_params]
                # Limit
                # Context bools
                
                params = [query_embedding, query_embedding, min_similarity] + where_params + \
                         [query, query] + where_params + \
                         [dedup_limit, include_context, include_context]
                
                cur.execute(sql, params)
                results = cur.fetchall()
                
                formatted_results = []
                for row in results:
                    content = row[0]
                    metadata = row[1]
                    mid = row[2]
                    sim = float(row[3]) if row[3] is not None else 0.0
                    kw_rank = float(row[4]) if row[4] is not None else 0.0
                    score = float(row[5])
                    prev_chunk = row[6]
                    next_chunk = row[7]
                    
                    # Construct window context
                    final_content = content
                    context_label = "Exact Match"
                    if prev_chunk or next_chunk:
                        context_parts = []
                        if prev_chunk: context_parts.append(f"[PREV] {prev_chunk}")
                        context_parts.append(f"[MATCH] {content}")
                        if next_chunk: context_parts.append(f"[NEXT] {next_chunk}")
                        final_content = "\n\n".join(context_parts)
                        context_label = "Context Window (±1 chunk)"
                    
                    # Parse metadata
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except:
                            pass
                            
                    citation_label = f"Meeting '{metadata.get('title', 'Unknown')}'"
                    if metadata.get('date'):
                        citation_label += f" on {metadata['date']}"
                    if metadata.get('speaker'):
                        citation_label += f" ({metadata['speaker']})"

                    formatted_results.append({
                        "content": final_content,
                        "metadata": metadata,
                        "meeting_id": mid,
                        "similarity": sim,
                        "keyword_rank": kw_rank,
                        "relevance_score": score,
                        "citation_label": citation_label,
                        "type": context_label
                    })

                # Dedup logic (same as before)
                if deduplicate:
                    meeting_results = {}
                    for result in formatted_results:
                        mid = result["meeting_id"]
                        if mid not in meeting_results:
                            meeting_results[mid] = []
                        if len(meeting_results[mid]) < max_results_per_meeting:
                            meeting_results[mid].append(result)
                    
                    final_list = []
                    for chunks in meeting_results.values():
                        final_list.extend(chunks)
                    
                    # Re-sort by RRF score
                    final_list.sort(key=lambda x: x["relevance_score"], reverse=True)
                    formatted_results = final_list

                formatted_results = formatted_results[:limit]
                
                # Add rank
                for i, res in enumerate(formatted_results):
                    res["rank"] = i + 1

        return {
            "success": True, 
            "results": formatted_results,
            "query": query,
            "total_results": len(formatted_results),
            "filters_applied": {
                "start_date": start_date,
                "end_date": end_date,
                "speaker": speaker,
                "RRF_enabled": True
            }
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    mcp.run()

