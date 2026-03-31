"""
core/metadata_extractor.py
---------------------------
Extracts opportunity_name and client_name from an ingested RFP document.

Robust version with:
  1. Wider RAG retrieval — fetches cover page, issuing org, and project description chunks
  2. Prompt explicitly handles "the Authority" placeholder pattern (Indian govt RFPs)
  3. Cleans "RFP for..." / "EOI for..." prefixes from opportunity names
  4. Retries on empty Ollama response
  5. Regex fallback if LLM fails entirely
  6. Never raises — always returns a dict
"""

import json
import re
import time
import ollama
from core.vector_store import retrieve

OLLAMA_MODEL = "llama3.2"
OLLAMA_HOST  = "http://localhost:11434"

MAX_RETRIES  = 3
RETRY_DELAY  = 2

# ── RAG queries — wider net to find org name and project description ──────────

METADATA_QUERIES = [
    "name of assignment project title",
    "request for proposal title heading",
    "procuring entity client organization name",
    "invitation to tender subject",
    "issued by ministry department authority organization",
    "NITI Aayog ministry department board corporation authority",
    "employer client funding agency",
    "background introduction project overview",
    "about the project program scheme",
]

# ── Main prompt — explicitly handles Indian govt RFP conventions ──────────────

METADATA_PROMPT = """You are an expert at reading Indian government and MDB RFP/tender documents.

IMPORTANT CONTEXT:
- Indian government RFPs often call the issuing body "the Authority", "the Employer", or "the Client" 
  throughout the document. You must find the ACTUAL organisation name — look for Ministry names, 
  Department names, Board names, Corporation names, Agency names, or funding bodies like ADB/World Bank.
- Opportunity names often start with "RFP for..." or "EOI for..." — strip that prefix and give the 
  actual assignment/project name only.

Read the following excerpts from a tender document and extract:
1. The ACTUAL NAME of the organisation that issued this tender (not "the Authority" — the real name)
2. The clean assignment/project name without "RFP for" or "EOI for" prefix

EXCERPTS:
{context}

Return ONLY valid JSON, no explanation, no markdown:
{{"opportunity_name": "clean project/assignment name without RFP/EOI prefix", "client_name": "actual organisation name e.g. NITI Aayog / Ministry of Road Transport / IHMCL / ADB"}}

Rules:
- client_name: MUST be a real organisation name. If document says "the Authority" look harder for 
  what that Authority actually is. Check for Ministry, Department, Corporation, Board, Agency names.
- opportunity_name: strip any "RFP for", "EOI for", "Tender for", "Selection of" prefix if present. 
  Give the core project/assignment description.
- If you truly cannot find the real org name, use null — do NOT use "the Authority" or "the Client"."""

# ── Fallback prompt — shorter, simpler ───────────────────────────────────────

FALLBACK_PROMPT = """Read this text from a government tender document.

TEXT:
{context}

Find:
1. The government ministry, department, board, corporation, or agency that issued this tender
2. The project or assignment name (without "RFP for" or "EOI for" prefix)

Reply with ONLY this JSON:
{{"opportunity_name": "project name", "client_name": "issuing organisation name"}}"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    return text[start:end] if start >= 0 and end > start else ""


def _clean_opportunity_name(name: str) -> str:
    """Strip common RFP header prefixes from opportunity names."""
    if not name:
        return name
    prefixes = [
        r"^RFP\s+for\s+",
        r"^EOI\s+for\s+",
        r"^Tender\s+for\s+",
        r"^Request\s+for\s+Proposal\s+for\s+",
        r"^Expression\s+of\s+Interest\s+for\s+",
        r"^Invitation\s+for\s+",
    ]
    for p in prefixes:
        name = re.sub(p, "", name, flags=re.IGNORECASE).strip()
    return name


def _is_placeholder_client(name: str) -> bool:
    """Returns True if the name is a generic placeholder, not a real org."""
    if not name:
        return True
    placeholders = {
        "the authority", "authority", "the client", "client",
        "the employer", "employer", "the owner", "owner",
        "null", "none", "n/a", "unknown", "the procuring entity",
        "procuring entity",
    }
    return name.strip().lower() in placeholders


def _parse_response(raw: str) -> dict:
    cleaned = _clean_json(raw)
    if not cleaned:
        return {}
    try:
        data = json.loads(cleaned)
        opp = data.get("opportunity_name") or None
        clt = data.get("client_name")      or None

        if opp:
            opp = _clean_opportunity_name(opp)
            if len(opp) < 5:
                opp = None

        if _is_placeholder_client(clt):
            clt = None
        elif clt and len(clt) < 3:
            clt = None

        return {"opportunity_name": opp, "client_name": clt}
    except Exception:
        return {}


def _call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
    try:
        client   = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )
        return response["message"]["content"] or ""
    except Exception as e:
        print(f"[MetaExtractor] Ollama error: {e}")
        return ""


def _regex_fallback(chunks: list) -> dict:
    """Regex extraction when LLM fails completely."""
    all_text = "\n".join(c["text"] for c in chunks[:10])
    opp = None
    clt = None

    title_patterns = [
        r"(?:Selection of|Appointment of|Hiring of)\s+(.{10,150}?)(?:\n|$)",
        r"(?:Project Management|Programme Management|Consulting Services?)\s+(?:for|to)\s+(.{10,120}?)(?:\n|$)",
        r"(?:Name of Assignment|Project Title|Assignment Title)\s*[:\-]\s*(.{5,200}?)(?:\n|$)",
        r"(?:Subject|RE|Re)\s*:\s*(.{10,150}?)(?:\n|$)",
        # Fallback: capture after "RFP for" but clean it
        r"RFP\s+for\s+(.{10,150}?)(?:\n|$)",
    ]

    client_patterns = [
        r"(NITI\s+Aayog[\w\s]*)",
        r"((?:Ministry|Department|Directorate)\s+of\s+[\w\s,]{3,60}?)(?:\n|$)",
        r"((?:Asian Development Bank|World Bank|UNDP|ADB|IFC|AIIB|JICA)[\w\s]*)",
        r"([\w\s]+(?:Corporation|Authority|Board|Council|Commission|Agency|Institute|Trust)[\w\s]{0,30})(?:\n|$)",
        r"(?:Issued by|Floated by|Published by)\s*[:\-]?\s*(.{3,100}?)(?:\n|$)",
        r"(?:Employer|Client|Procuring Entity)\s*[:\-]\s*(.{3,100}?)(?:\n|$)",
    ]

    for pat in title_patterns:
        m = re.search(pat, all_text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip().rstrip(".,;")
            if len(candidate) > 10:
                opp = _clean_opportunity_name(candidate)
                break

    for pat in client_patterns:
        m = re.search(pat, all_text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip().rstrip(".,;")
            if len(candidate) > 3 and not _is_placeholder_client(candidate):
                clt = candidate
                break

    if opp or clt:
        print(f"[MetaExtractor] Regex fallback — opp={opp!r} client={clt!r}")

    return {"opportunity_name": opp, "client_name": clt}


# ── Main entry point ──────────────────────────────────────────────────────────

def extract_metadata(doc_name: str, model: str = OLLAMA_MODEL) -> dict:
    """
    Extract opportunity_name and client_name. Never raises.

    Returns:
    {
        "opportunity_name": str or None,
        "client_name":      str or None,
        "error":            str or None,
    }
    """
    # ── Retrieve chunks — more queries, lower threshold to cast a wider net ───
    seen   = set()
    chunks = []
    for query in METADATA_QUERIES:
        for c in retrieve(query, doc_name=doc_name, top_k=3):
            key = c["clause_ref"] + str(c["page_no"])
            if key not in seen and c["score"] > 0.18:   # slightly lower threshold
                seen.add(key)
                chunks.append(c)

    chunks.sort(key=lambda x: x["score"], reverse=True)
    chunks = chunks[:10]

    if not chunks:
        return {"opportunity_name": None, "client_name": None,
                "error": "No chunks found in vector store."}

    # Prefer early pages (cover page has title + issuing org)
    top_chunks = sorted(chunks[:8], key=lambda x: (x["page_no"] or 999, -x["score"]))

    context = "\n\n---\n\n".join(
        f"[Page {c['page_no']} | {c['section_heading']}]\n{c['text']}"
        for c in top_chunks
    )

    # ── Attempt 1–3: Main prompt with retries ─────────────────────────────────
    result     = {}
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[MetaExtractor] Attempt {attempt}/{MAX_RETRIES}...")
        raw = _call_ollama(METADATA_PROMPT.format(context=context), model=model)

        if not raw.strip():
            last_error = f"Empty response on attempt {attempt}"
            print(f"[MetaExtractor] Empty response. {'Retrying...' if attempt < MAX_RETRIES else 'Trying fallback.'}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue

        result = _parse_response(raw)
        if result.get("opportunity_name") or result.get("client_name"):
            break
        else:
            last_error = f"Unusable response on attempt {attempt}"
            print(f"[MetaExtractor] Placeholder/unparseable result. {'Retrying...' if attempt < MAX_RETRIES else 'Trying fallback.'}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    # ── Attempt 4: Simpler fallback prompt ────────────────────────────────────
    if not result.get("opportunity_name") and not result.get("client_name"):
        print("[MetaExtractor] Trying simpler fallback prompt...")
        short_context = "\n\n".join(c["text"][:400] for c in top_chunks[:3])
        raw = _call_ollama(FALLBACK_PROMPT.format(context=short_context), model=model)
        if raw.strip():
            result = _parse_response(raw)
            if result.get("opportunity_name") or result.get("client_name"):
                print("[MetaExtractor] Fallback prompt succeeded.")

    # ── Attempt 5: Regex fallback (no LLM) ───────────────────────────────────
    if not result.get("opportunity_name") and not result.get("client_name"):
        print("[MetaExtractor] LLM failed — falling back to regex...")
        result = _regex_fallback(chunks)

    opp = result.get("opportunity_name")
    clt = result.get("client_name")

    print(f"[MetaExtractor] opportunity_name = {opp!r}")
    print(f"[MetaExtractor] client_name      = {clt!r}")

    return {
        "opportunity_name": opp,
        "client_name":      clt,
        "error":            last_error if not opp and not clt else None,
    }