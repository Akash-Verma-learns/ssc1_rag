"""
Vector Store
------------
Handles ChromaDB ingestion and semantic retrieval of RFP chunks.
Uses sentence-transformers for local, free embeddings (no API key needed).

Model used: all-MiniLM-L6-v2
  - 80MB, runs on CPU
  - Fast and accurate for legal/contract text retrieval
"""

import chromadb
from chromadb.utils import embedding_functions
from typing import List
from core.parser import Chunk


# ──────────────────────────────────────────────────────────────────────────────
# Singleton vector store (persists to disk)
# ──────────────────────────────────────────────────────────────────────────────

_client = None
_collection = None

COLLECTION_NAME = "rfp_chunks"
DB_PATH = "./chroma_db"  # persists across runs


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=DB_PATH)

        # Free local embeddings — no API key, no internet needed after first download
        embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embed_fn,
            metadata={"hnsw:space": "cosine"}  # cosine similarity for semantic search
        )
    return _collection


# ──────────────────────────────────────────────────────────────────────────────
# Ingest
# ──────────────────────────────────────────────────────────────────────────────

def ingest_chunks(chunks: List[Chunk], doc_id: str) -> int:
    """
    Ingest parsed chunks into ChromaDB.
    If this doc was ingested before, deletes old chunks first (upsert behavior).
    Returns count of chunks ingested.
    """
    collection = _get_collection()

    # Delete old chunks for this doc (re-upload scenario)
    try:
        existing = collection.get(where={"doc_name": doc_id})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            print(f"[VectorStore] Deleted {len(existing['ids'])} old chunks for '{doc_id}'")
    except Exception:
        pass  # collection is empty or filter not found

    if not chunks:
        return 0

    # Batch ingest (ChromaDB handles large batches well)
    batch_size = 100
    total = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        collection.add(
            ids=[c.chunk_id for c in batch],
            documents=[c.text for c in batch],
            metadatas=[
                {
                    "page_no": c.page_no,
                    "section_heading": c.section_heading,
                    "clause_ref": c.clause_ref,
                    "doc_name": c.doc_name,
                }
                for c in batch
            ],
        )
        total += len(batch)

    print(f"[VectorStore] Ingested {total} chunks for '{doc_id}'")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Retrieve
# ──────────────────────────────────────────────────────────────────────────────

def retrieve(query: str, doc_name: str, top_k: int = 5) -> List[dict]:
    """
    Semantic search for relevant chunks in a specific document.

    Returns list of:
    {
        "text": str,
        "page_no": int,
        "section_heading": str,
        "clause_ref": str,
        "score": float  (0–1, higher = more similar)
    }
    """
    collection = _get_collection()

    results = collection.query(
        query_texts=[query],
        n_results=top_k,
        where={"doc_name": doc_name},
        include=["documents", "metadatas", "distances"],
    )

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({
            "text": doc,
            "page_no": meta.get("page_no", 0),
            "section_heading": meta.get("section_heading", ""),
            "clause_ref": meta.get("clause_ref", ""),
            "score": round(1 - dist, 4),  # convert distance to similarity
        })

    # Sort by score descending
    output.sort(key=lambda x: x["score"], reverse=True)
    return output


def list_docs() -> List[str]:
    """List all document names currently in the vector store."""
    collection = _get_collection()
    all_meta = collection.get(include=["metadatas"])["metadatas"]
    return list({m["doc_name"] for m in all_meta if m})


def delete_doc(doc_name: str) -> int:
    """Remove all chunks for a specific document."""
    collection = _get_collection()
    existing = collection.get(where={"doc_name": doc_name})
    if existing["ids"]:
        collection.delete(ids=existing["ids"])
        return len(existing["ids"])
    return 0
