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
from google import genai
from google.genai import types
import cohere
from datetime import datetime, timezone
import json
from dateutil import parser as date_parser

load_dotenv()

# Initialize FastMCP
mcp = FastMCP("TLDV RAG Server")

# Initialize Pinecone
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
index_name = os.getenv("PINECONE_INDEX_NAME", "meeting-transcripts-v3")
index_host = os.getenv("PINECONE_HOST")
index = pc.Index(index_name, host=index_host) if index_host else pc.Index(index_name)

# Initialize Gemini
genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Initialize Cohere
cohere_api_key = os.getenv("COHERE_API_KEY") or os.getenv("COHERE_API")
cohere_client = cohere.Client(api_key=cohere_api_key)

def get_embedding(text: str) -> List[float]:
    """Generate embedding using Google Gemini model via new google-genai SDK."""
    model_name = os.getenv("PINECONE_MODEL", "text-embedding-004")
    # google-genai handles 'text-embedding-004' directly
    
    result = genai_client.models.embed_content(
        model=model_name,
        contents=text,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=768
        )
    )
    # result.embeddings is a list of Embeddings objects if contents is a list, 
    # or a single Embedding object if contents is a string (actually it's always a list in some versions)
    # Let's check the structure. If it's a list, we take the first one.
    if isinstance(result.embeddings, list):
        return result.embeddings[0].values
    return result.embeddings.values

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
    base_filter = {"source_type": "tldv transcripts"}
    
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
    base_filter = {"source_type": "google_drive"}

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
                doc_speakers = metadata.get("speakers")
                if not doc_speakers:
                    continue
                
                # Handle list or string
                if isinstance(doc_speakers, list):
                    if not any(speaker.lower() in s.lower() for s in doc_speakers):
                        continue
                elif isinstance(doc_speakers, str):
                    if speaker.lower() not in doc_speakers.lower():
                        continue
                else:
                    # Fallback for unexpected types
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
            # Use Cohere reranker API
            passages = [c["content"] for c in candidates]
            
            # Call Cohere rerank API
            rerank_response = cohere_client.rerank(
                query=query,
                documents=passages,
                model="rerank-english-v3.0",
                top_n=len(passages)  # Get scores for all candidates
            )
            
            # Map reranked results back to candidates
            for result in rerank_response.results:
                candidates[result.index]["relevance_score"] = float(result.relevance_score)
                
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
            
            # Simplified labeling: both use 'name' in the new index
            title = meta.get("name") or meta.get("file_name") or "Unknown"
            
            if source_key == "meeting_id":
                 m_date = meta.get("date") or meta.get("creation_date") or "Unknown Date"
                 res["citation_label"] = f"Meeting '{title}' ({m_date})"
            else:
                 res["citation_label"] = f"Document '{title}'"

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
