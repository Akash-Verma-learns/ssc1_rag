"""
Clause Extractor
----------------
Uses Ollama (local, free) to extract structured data from RFP clause text.
The LLM extracts FACTS. The risk_engine.py evaluates RISK.

Supported models (via Ollama):
  - llama3.2        (recommended: fast, good at structured extraction)
  - gemma3          (alternative)
  - mistral         (alternative)
  - phi4            (lighter, for low-RAM machines)

Install Ollama: https://ollama.com
Then: ollama pull llama3.2
"""

import json
import re
import ollama
from typing import Optional
from core.vector_store import retrieve


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

OLLAMA_MODEL = "llama3.2"   # change to "gemma3" or "mistral" if needed
OLLAMA_HOST = "http://localhost:11434"  # default Ollama host

# GTBL context injected into eligibility prompts
GTBL_CONTEXT = """
IMPORTANT CONTEXT ABOUT GTBL (the bidding firm):
- GTBL was blacklisted/debarred from October 2021 to September 2024.
- As of today, GTBL faces a penalty for non-performance.
- GTBL was previously terminated by a client for contractual breach/unsatisfactory performance.
  This has since been converted to an amicable closure effective 09.01.2026.
Use this factual position when evaluating eligibility declarations.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Prompt templates per clause type
# ──────────────────────────────────────────────────────────────────────────────

EXTRACTION_PROMPTS = {

    "liability": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the limitation of liability clause",
  "clause_reference": "clause number or section reference (e.g. Clause 4.1)",
  "page_no": "page number if visible, else null",
  "cap_info": "description of the liability cap - e.g. 'contract value', 'uncapped', '2x contract value', '50% of fees'",
  "is_uncapped": true or false,
  "notes": "any additional relevant observations"
}}
If the clause is not found, return {{"clause_text": null, "cap_info": "not found"}}.
""",

    "insurance": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the insurance clause",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "client_is_coinsured": true or false,
  "requires_client_approval": true or false,
  "flags": ["list any high-risk conditions found"],
  "notes": "any additional relevant observations"
}}
If not found, return {{"clause_text": null}}.
""",

    "scope": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the scope of work",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "summary": "3-5 sentence summary of what the consultant/firm is required to do",
  "high_risk_activities": [
    "list only activities that are high-risk: civil works, DPR, supervision, third-party verification, legal services, AI decision-making, gambling, safety of lives, approving grants/payments"
  ],
  "firm_type_required": "consulting firm / audit firm / architectural firm / other",
  "notes": "any additional observations"
}}
""",

    "payment": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the payment terms",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "payment_structure": "milestone-based / deliverable-based / deployment-based / monthly / quarterly / annual / mixed",
  "invoice_to_payment_days": number or null,
  "has_invoice_cycle": true or false,
  "deliverable_approval_days": number or null,
  "has_approval_timeline": true or false,
  "notes": "any additional observations"
}}
""",

    "deliverables": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "summary of deliverables and timelines",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "deliverables_list": ["list each deliverable with its timeline"],
  "flags": [
    "list any of these if found: overlapping deliverables, unclear acceptance criteria, aggressive timelines, missing client dependencies"
  ],
  "issues": "overall assessment of deliverable risks",
  "notes": "any additional observations"
}}
""",

    "personnel": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the personnel/staffing clause",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "replacement_days": number or null,
  "replacement_conditions": "conditions under which replacement is allowed",
  "penalties_for_non_compliance": "any penalties mentioned",
  "notes": "any additional observations"
}}
""",

    "ld": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the liquidated damages clause",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "ld_cap_text": "description of LD cap e.g. '10% of contract value', 'uncapped', '20% of fees'",
  "ld_cap_percentage": number or null,
  "ld_triggers": ["scenarios where LDs apply"],
  "is_uncapped": true or false,
  "notes": "any additional observations"
}}
""",

    "penalties": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the penalty clause",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "ld_cap_text": "description of penalty cap e.g. '10% of contract value', 'uncapped'",
  "ld_cap_percentage": number or null,
  "ld_triggers": ["scenarios where penalties apply"],
  "is_uncapped": true or false,
  "notes": "any additional observations"
}}
""",

    "termination": """
You are a legal contract analyst. Read the following clause(s) from an RFP/tender document.

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of termination clauses",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "client_termination_rights": "describe client's termination rights",
  "gtbl_termination_rights": "describe consultant/firm's termination rights (if any)",
  "gtbl_can_terminate": true or false,
  "is_unilateral": true or false,
  "recovery_of_past_payments": true or false,
  "notes": "any additional observations"
}}
""",

    "eligibility": """
You are a legal contract analyst.
{gtbl_context}

CLAUSES:
{context}

Extract the following and return ONLY valid JSON (no explanation):
{{
  "clause_text": "verbatim text of the eligibility clause / declaration",
  "clause_reference": "clause number or section reference",
  "page_no": "page number if visible, else null",
  "declaration_type": "blacklisting / termination / penalty / combined",
  "uses_historical_language": true or false,
  "historical_language_examples": ["exact phrases like 'has not been blacklisted'"],
  "is_no_deviation": true or false,
  "conflicts_with_gtbl_position": true or false,
  "suggested_deviation": "if historical language used, suggest minimum change to 'is not' / 'as on date' language",
  "notes": "any additional observations"
}}
""",
}

# RAG queries to find the right chunks for each clause type
RAG_QUERIES = {
    "liability": [
        "limitation of liability clause",
        "liability cap contract value",
        "unlimited liability indemnification",
    ],
    "insurance": [
        "insurance requirements clause",
        "co-insured professional indemnity",
        "insurance policies approval",
    ],
    "scope": [
        "scope of work services",
        "terms of reference deliverables",
        "scope of assignment consultant",
    ],
    "payment": [
        "payment terms invoice",
        "payment schedule milestone fees",
        "invoice payment cycle days",
    ],
    "deliverables": [
        "deliverables submission timeline",
        "reports deliverables schedule",
        "acceptance of deliverables criteria",
    ],
    "personnel": [
        "key personnel replacement substitution",
        "staff replacement period",
        "personnel change requirements",
    ],
    "ld": [
        "liquidated damages clause",
        "LD delay penalty contract value",
        "liquidated damages cap percentage",
    ],
    "penalties": [
        "penalty clause non-performance",
        "penalties breach of contract",
        "financial penalties triggers",
    ],
    "termination": [
        "termination clause rights",
        "termination for convenience default",
        "contract termination consultant",
    ],
    "eligibility": [
        "eligibility criteria blacklisting debarment",
        "declaration undertaking sanctioned",
        "no adverse record eligibility",
        "termination penalty declaration",
        "no deviation clause unconditional acceptance",
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Core extractor
# ──────────────────────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    """Strip markdown code fences from LLM output."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    text = text.strip("`").strip()
    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        return text[start:end]
    return text


def extract_clause(
    clause_type: str,
    doc_name: str,
    top_k: int = 6,
    model: str = OLLAMA_MODEL,
) -> dict:
    """
    Full RAG + LLM extraction pipeline for a single clause type.

    1. Retrieve top-k relevant chunks from ChromaDB
    2. Feed chunks + prompt to local Ollama LLM
    3. Parse structured JSON response
    4. Return extracted data dict

    Returns dict with extracted fields + metadata:
    {
        "clause_type": str,
        "doc_name": str,
        "retrieved_chunks": [...],   # raw chunks for audit trail
        "extracted": {...},          # LLM-extracted structured data
        "error": str or None,
    }
    """
    if clause_type not in EXTRACTION_PROMPTS:
        raise ValueError(f"Unknown clause type '{clause_type}'. Options: {list(EXTRACTION_PROMPTS.keys())}")

    # ── Step 1: Multi-query RAG retrieval ──────────────────────────────────────
    queries = RAG_QUERIES.get(clause_type, [clause_type])
    seen_ids = set()
    all_chunks = []

    for query in queries:
        chunks = retrieve(query, doc_name=doc_name, top_k=3)
        for chunk in chunks:
            cid = chunk["clause_ref"] + str(chunk["page_no"])
            if cid not in seen_ids and chunk["score"] > 0.25:
                seen_ids.add(cid)
                all_chunks.append(chunk)

    # Sort by relevance and take top_k
    all_chunks.sort(key=lambda x: x["score"], reverse=True)
    all_chunks = all_chunks[:top_k]

    if not all_chunks:
        return {
            "clause_type": clause_type,
            "doc_name": doc_name,
            "retrieved_chunks": [],
            "extracted": {"clause_text": None, "clause_reference": "Not found", "page_no": None},
            "error": "No relevant chunks found in document. Clause may not exist.",
        }

    # ── Step 2: Build context string ──────────────────────────────────────────
    context_parts = []
    for c in all_chunks:
        context_parts.append(
            f"[Page {c['page_no']} | {c['section_heading']} | Ref: {c['clause_ref']}]\n{c['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    # ── Step 3: Build prompt ───────────────────────────────────────────────────
    prompt_template = EXTRACTION_PROMPTS[clause_type]
    if clause_type == "eligibility":
        prompt = prompt_template.format(context=context, gtbl_context=GTBL_CONTEXT)
    else:
        prompt = prompt_template.format(context=context)

    # ── Step 4: Call Ollama ────────────────────────────────────────────────────
    try:
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},  # deterministic extraction
        )
        raw_output = response["message"]["content"]
        json_str = _clean_json(raw_output)
        extracted = json.loads(json_str)

    except ollama.ResponseError as e:
        return {
            "clause_type": clause_type,
            "doc_name": doc_name,
            "retrieved_chunks": all_chunks,
            "extracted": {"clause_text": None, "clause_reference": "LLM Error"},
            "error": f"Ollama error: {e}. Is Ollama running? Try: ollama serve",
        }
    except json.JSONDecodeError as e:
        return {
            "clause_type": clause_type,
            "doc_name": doc_name,
            "retrieved_chunks": all_chunks,
            "extracted": {"clause_text": raw_output, "clause_reference": "JSON parse error"},
            "error": f"Could not parse LLM output as JSON: {e}. Raw: {raw_output[:200]}",
        }

    return {
        "clause_type": clause_type,
        "doc_name": doc_name,
        "retrieved_chunks": all_chunks,
        "extracted": extracted,
        "error": None,
    }


def extract_all_clauses(doc_name: str, model: str = OLLAMA_MODEL) -> dict:
    """
    Run extraction for all 10 clause types and return combined results.
    """
    results = {}
    clause_types = list(EXTRACTION_PROMPTS.keys())

    print(f"\n[Extractor] Processing {len(clause_types)} clauses for '{doc_name}'...")
    for i, ctype in enumerate(clause_types, 1):
        print(f"  [{i}/{len(clause_types)}] Extracting: {ctype}...")
        results[ctype] = extract_clause(ctype, doc_name, model=model)
        if results[ctype]["error"]:
            print(f"    ⚠ Warning: {results[ctype]['error']}")
        else:
            print(f"    ✓ Done")

    return results
