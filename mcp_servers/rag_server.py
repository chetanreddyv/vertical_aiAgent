"""
FastMCP RAG Server (Advanced Pinecone Version)
Exposes a tool to search for meeting context using Pinecone, sentence-transformers,
and advanced RAG techniques like recency weightage and deduplication.
"""

from fastmcp import FastMCP
from typing import Dict, Any, List, Optional
import os
from dotenv import load_dotenv
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer
from datetime import datetime, timezone
import json
from dateutil import parser as date_parser

load_dotenv()

# Initialize FastMCP
mcp = FastMCP("TLDV RAG Server")

# Initialize Pinecone
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
index_name = os.getenv("PINECONE_INDEX_NAME", "drive-rag")
index = pc.Index(index_name)

# Initialize Embedding Model
# Lazy-load model to prevent MCP timeout during startup
_embedding_model = None

def get_embedding(text: str) -> List[float]:
    """Generate embedding for the given text using local sentence-transformers."""
    global _embedding_model
    if _embedding_model is None:
        model_name = os.getenv("PINECONE_MODEL", "all-MiniLM-L6-v2")
        _embedding_model = SentenceTransformer(model_name)
    
    embedding = _embedding_model.encode(text)
    return embedding.tolist()

def calculate_recency_score(match_date_str: Optional[str]) -> float:
    """
    Calculate a recency boost factor based on the age of the document.
    Returns a multiplier (1.0 for very recent, declining as age increases).
    """
    if not match_date_str:
        return 1.0
    
    try:
        match_date = date_parser.parse(match_date_str)
        if match_date.tzinfo is None:
            match_date = match_date.replace(tzinfo=timezone.utc)
            
        now = datetime.now(timezone.utc)
        delta = now - match_date
        days_old = max(0, delta.days)
        
        # Time decay factor: 1 / (1 + age_factor * days)
        # 0.005 means a 200 day old document is at ~0.5 weight
        decay = 1.0 / (1.0 + (days_old * 0.005))
        return decay
    except Exception:
        return 1.0

def calculate_keyword_boost(text: str, query: str) -> float:
    """
    Apply a simple keyword boost if query terms appear in the text.
    Returns a multiplier (1.0 to 1.2).
    """
    query_terms = [t.lower() for t in query.split() if len(t) > 3]
    if not query_terms:
        return 1.0
    
    text_lower = text.lower()
    matches = sum(1 for term in query_terms if term in text_lower)
    
    # max boost of 20% for finding multiple query terms
    boost = 1.0 + (min(matches, 4) * 0.05)
    return boost

@mcp.tool
def search_meetings(
    query: str, 
    limit: int = 10,
    min_similarity: float = 0.3,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speaker: Optional[str] = None,
    meeting_id: Optional[str] = None,
    deduplicate: bool = True,
    max_results_per_meeting: int = 3,
    apply_recency_boost: bool = True
) -> Dict[str, Any]:
    """
    Advanced search for meeting context using Pinecone with recency weighting and deduplication.
    
    Args:
        query: Natural language query
        limit: Max number of final results
        min_similarity: Minimum score threshold (default 0.3)
        start_date: ISO date filter
        end_date: ISO date filter
        speaker: Filter results by speaker name (partial match)
        meeting_id: Filter results by specific meeting ID
        deduplicate: If True, limits results per meeting to ensure diversity
        max_results_per_meeting: Max chunks allowed from a single meeting if deduplicating
        apply_recency_boost: If True, boosts newer meetings in the results
    """
    try:
        # Convert query to embedding
        query_embedding = get_embedding(query)
        
        # Build Pinecone-side filters
        filter_dict = {}
        if meeting_id:
            filter_dict["meeting_id"] = meeting_id
            
        # Execute search (fetch more for post-processing)
        fetch_limit = limit * 5 if deduplicate else limit * 2
        results = index.query(
            vector=query_embedding,
            top_k=min(fetch_limit, 100),
            include_metadata=True,
            filter=filter_dict if filter_dict else None
        )
        
        # Convert start/end date strings for filtering
        s_date = date_parser.parse(start_date).replace(tzinfo=timezone.utc) if start_date else None
        e_date = date_parser.parse(end_date).replace(tzinfo=timezone.utc) if end_date else None
        
        processed_hits = []
        for match in results.matches:
            metadata = match.metadata or {}
            content = metadata.get("text", "")
            match_score = match.score
            
            # 1. Date range filtering (Python side)
            match_dt = None
            if metadata.get("date"):
                try:
                    match_dt = date_parser.parse(metadata["date"])
                    if match_dt.tzinfo is None:
                        match_dt = match_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            
            if s_date and match_dt and match_dt < s_date:
                continue
            if e_date and match_dt and match_dt > e_date:
                continue
                
            # 2. Speaker filtering (Python side)
            if speaker and speaker.lower() not in metadata.get("speakers", "").lower():
                continue
                
            # 3. Apply Boosts
            final_relevance = match_score
            
            # Recency boost
            recency_factor = 1.0
            if apply_recency_boost:
                recency_factor = calculate_recency_score(metadata.get("date"))
                final_relevance *= (0.7 + (0.3 * recency_factor)) # 30% range based on recency
            
            # Keyword boost (Pseudo-Hybrid)
            keyword_factor = calculate_keyword_boost(content, query)
            final_relevance *= keyword_factor
            
            # 4. Global threshold check (on base similarity)
            if match_score < min_similarity:
                continue
            
            processed_hits.append({
                "content": content,
                "metadata": metadata,
                "meeting_id": metadata.get("meeting_id"),
                "similarity": match_score,
                "recency_factor": recency_factor,
                "keyword_boost": keyword_factor,
                "relevance_score": final_relevance,
                "citation_label": f"Meeting '{metadata.get('name', 'Unknown')}' ({metadata.get('date', 'Unknown')})",
                "type": "Pinecone Advanced Hybrid"
            })

        # Sort by relevance score
        processed_hits.sort(key=lambda x: x["relevance_score"], reverse=True)

        # 5. Deduplication logic
        final_results = []
        if deduplicate:
            meeting_counts = {}
            for hit in processed_hits:
                mid = hit["meeting_id"] or "unknown"
                meeting_counts[mid] = meeting_counts.get(mid, 0) + 1
                if meeting_counts[mid] <= max_results_per_meeting:
                    final_results.append(hit)
        else:
            final_results = processed_hits

        # Final cut and rank
        final_results = final_results[:limit]
        for i, res in enumerate(final_results):
            res["rank"] = i + 1

        return {
            "success": True, 
            "results": final_results,
            "query": query,
            "total_results": len(final_results),
            "info": {
                "recency_applied": apply_recency_boost,
                "deduplicated": deduplicate,
                "hits_analyzed": len(results.matches)
            }
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    mcp.run()
