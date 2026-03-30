"""
core/metadata_extractor.py
---------------------------
Extracts high-level RFP metadata using the same RAG + Ollama approach
as the clause extractor:
  - opportunity_name  : official name/title of the assignment
  - client_name       : name of the client/procuring entity

Called at the START of the pipeline, before clause extraction.
Results are saved back to the RFP DB record if those fields are blank.
"""

import json
import re
import ollama
from core.vector_store import retrieve

OLLAMA_MODEL = "llama3.2"
OLLAMA_HOST  = "http://localhost:11434"

# ── RAG queries to find title / client chunks ─────────────────────────────────

METADATA_QUERIES = [
    "name of assignment project title",
    "request for proposal title heading",
    "procuring entity client organization name",
    "invitation to tender subject",
    "scope of assignment name",
    "consulting firm invited proposal",
    "employer client funding agency",
]

METADATA_PROMPT = """
You are an expert at reading RFP/tender documents.
Read the following excerpts from a tender document and extract:
1. The official NAME / TITLE of this assignment or project
2. The CLIENT / PROCURING ENTITY / EMPLOYER who issued this tender

EXCERPTS:
{context}

Return ONLY valid JSON, no explanation:
{{
  "opportunity_name": "full official name of the assignment or project",
  "client_name": "name of the client, procuring entity, or employer"
}}

Rules:
- opportunity_name: use the exact official project title (e.g. "Preparation of Sustainable Urban Mobility Plan for XYZ City")
- client_name: the organisation issuing the tender (e.g. "Asian Development Bank", "Ministry of Housing", "World Bank")
- If you cannot determine either value with confidence, use null for that field
- Do NOT include reference numbers, dates, or bid IDs in opportunity_name
"""


def _clean_json(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    return text[start:end] if start >= 0 and end > start else text


def extract_metadata(doc_name: str, model: str = OLLAMA_MODEL) -> dict:
    """
    Extract opportunity_name and client_name from an already-ingested document.

    Returns:
    {
        "opportunity_name": str or None,
        "client_name":      str or None,
        "error":            str or None,
    }
    """
    # ── Retrieve relevant chunks ──────────────────────────────────────────────
    seen = set()
    chunks = []
    for query in METADATA_QUERIES:
        for c in retrieve(query, doc_name=doc_name, top_k=3):
            key = c["clause_ref"] + str(c["page_no"])
            if key not in seen and c["score"] > 0.20:
                seen.add(key)
                chunks.append(c)

    chunks.sort(key=lambda x: x["score"], reverse=True)
    chunks = chunks[:8]  # top 8 most relevant chunks

    if not chunks:
        return {"opportunity_name": None, "client_name": None,
                "error": "No chunks found for metadata extraction"}

    # Prefer first few pages (title / cover page likely there)
    chunks.sort(key=lambda x: (x["page_no"] or 999, -x["score"]))
    chunks = chunks[:6]

    context = "\n\n---\n\n".join(
        f"[Page {c['page_no']} | {c['section_heading']}]\n{c['text']}"
        for c in chunks
    )

    # ── Call Ollama ───────────────────────────────────────────────────────────
    try:
        client   = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": METADATA_PROMPT.format(context=context)}],
            options={"temperature": 0.0},
        )
        raw = response["message"]["content"]
        data = json.loads(_clean_json(raw))

        opp  = data.get("opportunity_name") or None
        clt  = data.get("client_name")      or None

        # Sanity check — reject obviously wrong extractions
        if opp and len(opp) < 5:
            opp = None
        if clt and len(clt) < 3:
            clt = None

        print(f"[MetaExtractor] opportunity_name={opp!r}")
        print(f"[MetaExtractor] client_name={clt!r}")

        return {"opportunity_name": opp, "client_name": clt, "error": None}

    except ollama.ResponseError as e:
        return {"opportunity_name": None, "client_name": None,
                "error": f"Ollama error: {e}"}
    except (json.JSONDecodeError, Exception) as e:
        return {"opportunity_name": None, "client_name": None,
                "error": f"Parse error: {e}"}