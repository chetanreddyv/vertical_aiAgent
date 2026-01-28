"""
FastMCP RAG Server (Corrected & Optimized)
Features:
- Hybrid-style pipeline: Dense Retrieval -> Python Filtering -> Re-ranking
- Solves 'Context Precision' issues via Cross-Encoder
- Solves 'Missing Data' issues via Pinecone-side date filtering
"""

from fastmcp import FastMCP
from typing import Dict, Any, List, Optional
import os
from dotenv import load_dotenv
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer, CrossEncoder # Added CrossEncoder
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

# --- Global Models (Lazy Loaded) ---
_embedding_model = None
_reranker_model = None

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        # Upgraded default model from MiniLM-L6 to mpnet-base for better semantic capture
        model_name = os.getenv("PINECONE_MODEL", "all-mpnet-base-v2") 
        print(f"Loading Embedding Model: {model_name}...")
        _embedding_model = SentenceTransformer(model_name)
    return _embedding_model

def get_reranker_model():
    global _reranker_model
    if _reranker_model is None:
        # Specialized model for re-sorting retrieved results
        model_name = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        print(f"Loading Reranker Model: {model_name}...")
        _reranker_model = CrossEncoder(model_name)
    return _reranker_model

def get_embedding(text: str) -> List[float]:
    model = get_embedding_model()
    # Normalize embeddings for cosine similarity
    return model.encode(text, normalize_embeddings=True).tolist()

@mcp.tool
def pinecone_index_info() -> Dict[str, Any]:
    """Retrieve Pinecone index configuration and statistics."""
    desc = pc.describe_index(name=index_name)
    stats = index.describe_index_stats()
    return {
        "index_name": index_name,
        "describe_index": desc.to_dict() if hasattr(desc, 'to_dict') else str(desc),
        "describe_index_stats": stats.to_dict() if hasattr(stats, 'to_dict') else str(stats)
    }

@mcp.tool
def search_meetings(
    query: str, 
    limit: int = 10,
    min_similarity: float = 0.2, 
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speaker: Optional[str] = None,
    meeting_id: Optional[str] = None,
    deduplicate: bool = True,
    max_results_per_meeting: int = 3
) -> Dict[str, Any]:
    """
    Search specifically for meetings (transcripts, summaries).
    Forces source_type='database' filter.
    """
    # Base filter: Must be a meeting
    base_filter = {"source_type": "database"}
    
    # Specific meeting filter
    if meeting_id:
        base_filter["meeting_id"] = meeting_id

    return _execute_rag_search(
        query=query,
        limit=limit,
        min_similarity=min_similarity,
        filter_dict=base_filter,
        start_date=start_date,
        end_date=end_date,
        speaker=speaker,
        deduplicate=deduplicate,
        max_results_per_source=max_results_per_meeting,
        source_key="meeting_id"
    )

@mcp.tool
def search_documents(
    query: str,
    limit: int = 10,
    min_similarity: float = 0.2,
    deduplicate: bool = True,
    max_results_per_document: int = 3
) -> Dict[str, Any]:
    """
    Search for documents (PDFs, Docs, Text files) in the knowledge base.
    Excludes meetings.
    """
    # Base filter: explicit exclusion of database (meetings) or check for absence of meeting_id
    # We found existing docs don't have source_type, or it's not 'database'
    base_filter = {"source_type": {"$ne": "database"}}

    return _execute_rag_search(
        query=query,
        limit=limit,
        min_similarity=min_similarity,
        filter_dict=base_filter,
        deduplicate=deduplicate,
        max_results_per_source=max_results_per_document,
        source_key="doc_id"
    )

def _execute_rag_search(
    query: str,
    limit: int,
    min_similarity: float,
    filter_dict: Dict[str, Any],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speaker: Optional[str] = None,
    deduplicate: bool = True,
    max_results_per_source: int = 3,
    source_key: str = "meeting_id"
) -> Dict[str, Any]:
    """
    Internal helper executing the standard RAG pipeline:
    Embed -> Query Pinecone -> Python Filter -> Rerank -> Deduplicate
    """
    try:
        # 1. Generate Query Embedding
        query_embedding = get_embedding(query)
        
        # 2. Execute Search (Fetch MORE for Reranking)
        fetch_limit = 150 
        
        results = index.query(
            vector=query_embedding,
            top_k=fetch_limit,
            include_metadata=True,
            filter=filter_dict
        )
        
        # 3. Initial Processing & Python-side Filtering
        s_date = date_parser.parse(start_date).replace(tzinfo=timezone.utc) if start_date else None
        e_date = date_parser.parse(end_date).replace(tzinfo=timezone.utc) if end_date else None
        
        candidates = []
        
        for match in results.matches:
            metadata = match.metadata or {}
            content = metadata.get("text", "")
            
            # Fallback for structured content
            if not content and "_node_content" in metadata:
                try:
                    node_content = json.loads(metadata["_node_content"])
                    content = node_content.get("text", "")
                except Exception:
                    pass

            # Date filtering
            match_dt = None
            # Try 'date' (meetings) or 'creation_date' (docs)
            date_str = metadata.get("date") or metadata.get("creation_date")
            if date_str:
                try:
                    match_dt = date_parser.parse(date_str)
                    if match_dt.tzinfo is None:
                        match_dt = match_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            
            if s_date and match_dt and match_dt < s_date:
                continue
            if e_date and match_dt and match_dt > e_date:
                continue

            # Speaker Filtering (Only for meetings usually)
            if speaker:
                doc_speakers = metadata.get("speakers", "")
                if not doc_speakers or speaker.lower() not in doc_speakers.lower():
                    continue

            # Initial threshold check (loose)
            if match.score < min_similarity:
                continue
                
            candidates.append({
                "content": content,
                "metadata": metadata,
                "source_id": metadata.get(source_key) or metadata.get("doc_id"),
                "initial_score": match.score,
                "id": match.id
            })

        # 4. RERANKING STEP
        if candidates:
            reranker = get_reranker_model()
            passages = [c["content"] for c in candidates]
            query_passage_pairs = [[query, p] for p in passages]
            rerank_scores = reranker.predict(query_passage_pairs)
            
            for i, candidate in enumerate(candidates):
                candidate["relevance_score"] = float(rerank_scores[i])
                
            # Sort by RERANKER score
            candidates.sort(key=lambda x: x["relevance_score"], reverse=True)
        else:
            # Fallback if no candidates (though loop wouldn't run)
            pass
        
        # 5. Deduplication
        final_results = []
        if deduplicate:
            source_counts = {}
            for hit in candidates:
                sid = hit["source_id"] or "unknown"
                source_counts[sid] = source_counts.get(sid, 0) + 1
                if source_counts[sid] <= max_results_per_source:
                    final_results.append(hit)
        else:
            final_results = candidates

        # 6. Final Cut
        final_results = final_results[:limit]
        
        # Add display formatting
        for i, res in enumerate(final_results):
            res["rank"] = i + 1
            meta = res["metadata"]
            if source_key == "meeting_id":
                 res["citation_label"] = f"Meeting '{meta.get('name', 'Unknown')}' ({meta.get('date', 'Unknown')})"
            else:
                 res["citation_label"] = f"Document '{meta.get('file_name', 'Unknown')}'"

        return {
            "success": True, 
            "results": final_results,
            "query": query,
            "total_results": len(final_results),
            "info": {
                "deduplicated": deduplicate,
                "hits_retrieved": len(results.matches),
                "hits_reranked": len(candidates)
            }
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    mcp.run()
