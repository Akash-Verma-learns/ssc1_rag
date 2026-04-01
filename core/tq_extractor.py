"""
TQ (Technical Qualification) Extractor — SSC2 Module
------------------------------------------------------
Two-stage pipeline:

STAGE 1 — Scoring criteria extraction from the RFP
  Uses RAG on the already-ingested RFP (ChromaDB) to locate the technical
  evaluation / scoring table. The LLM parses it into structured criteria:
    [{parameter, max_marks, item_code, sub_items: [...], criteria_text}]

STAGE 2 — Proposal evaluation
  The uploaded proposal (PDF/DOCX) is parsed and ingested into a separate
  ChromaDB namespace (doc_name = "proposal_{job_id}_{filename}").
  For each scoring criterion the LLM reads relevant proposal sections and
  assigns a score with a justification.

Returns a structured result ready for DB persistence and API response.
"""

import json
import re
import ollama
from pathlib import Path
from typing import List, Optional

from core.vector_store import retrieve, ingest_chunks
from core.parser import parse_document

OLLAMA_MODEL = "llama3.2"
OLLAMA_HOST  = "http://localhost:11434"

# ── RAG queries used to find the scoring/evaluation table in the RFP ──────────

SCORING_TABLE_QUERIES = [
    "technical evaluation scoring criteria marks",
    "scoring criteria maximum marks parameter",
    "evaluation criteria point allocation",
    "technical proposal marking scheme",
    "grand total marks evaluation",
    "criteria for evaluation of technical proposal",
    "item code parameter maximum marks criteria table",
    "eligible assignments scoring",
    "key personnel marks experience",
]

# ── Prompt: extract scoring criteria from RFP chunks ──────────────────────────

CRITERIA_EXTRACTION_PROMPT = """You are an expert at reading government RFP/tender documents.

Read the following excerpts from an RFP and extract the COMPLETE technical evaluation
scoring table. This table lists parameters, maximum marks, and evaluation criteria.

EXCERPTS:
{context}

Extract every scoring criterion and return ONLY valid JSON (no explanation, no markdown):
{{
  "evaluation_title": "name of this evaluation e.g. Technical Proposal Evaluation",
  "grand_total_marks": <number, typically 100>,
  "criteria": [
    {{
      "item_code": "1" or "1a" or "3(a)" etc,
      "parameter": "parameter name e.g. Relevant Experience of the Applicant",
      "max_marks": <number>,
      "criteria_text": "full criteria description verbatim",
      "is_sub_item": false,
      "parent_item_code": null,
      "sub_items": [
        {{
          "item_code": "3(a)",
          "parameter": "Project Director & Team Leader",
          "max_marks": 20,
          "criteria_text": "same criteria as parent unless specified",
          "is_sub_item": true,
          "parent_item_code": "3"
        }}
      ]
    }}
  ]
}}

Rules:
- If a parameter has sub-items (e.g. "3(a)", "3(b)"), include them inside sub_items[] of the parent.
- Capture marks for BOTH parent items AND sub-items.
- If sub-items exist, the parent max_marks should be the sum of sub-items.
- Include criteria_text verbatim from the document.
- grand_total_marks is usually 100.
- If no scoring table is found, return {{"criteria": [], "grand_total_marks": 0}}.
"""

# ── Prompt: evaluate one criterion against the proposal ───────────────────────

CRITERION_EVALUATION_PROMPT = """You are a technical evaluation expert reviewing a proposal against RFP criteria.

EVALUATION CRITERION:
Parameter: {parameter}
Maximum Marks: {max_marks}
Evaluation Criteria: {criteria_text}

RELEVANT SECTIONS FROM THE PROPOSAL:
{proposal_context}

Based ONLY on what is present in the proposal excerpts above, evaluate this criterion.

Return ONLY valid JSON (no explanation):
{{
  "score": <number between 0 and {max_marks}>,
  "score_percentage": <0-100>,
  "justification": "2-4 sentences explaining why this score was awarded, referencing specific proposal content",
  "strengths": ["list of 1-3 specific strengths found in the proposal for this criterion"],
  "gaps": ["list of 0-3 specific gaps or missing information"],
  "evidence_found": true or false
}}

Scoring guidance:
- 90-100% of max: Exceptional, all criteria comprehensively addressed
- 70-89%: Good, most criteria well addressed with minor gaps
- 50-69%: Adequate, key criteria addressed but notable gaps
- 30-49%: Weak, partial coverage of criteria
- 0-29%: Poor or not addressed

Be conservative — only award marks for content that is explicitly present.
If proposal context is empty or irrelevant, award 0 and set evidence_found to false.
"""

# ── RAG queries for proposal evaluation per criterion type ────────────────────

PROPOSAL_QUERIES_BY_PATTERN = {
    "experience": [
        "firm experience assignments completed",
        "previous projects undertaken",
        "relevant assignments eligible",
        "company experience work history",
        "similar assignments undertaken",
    ],
    "methodology": [
        "methodology approach work plan",
        "proposed methodology technical approach",
        "work plan implementation strategy",
        "project methodology phases activities",
        "technical approach deliverables timeline",
    ],
    "personnel": [
        "key personnel team members qualifications",
        "team leader project director experience",
        "curriculum vitae CV professional experience",
        "expert qualifications assignments worked",
        "staff experience eligible assignments",
    ],
    "turnover": [
        "firm turnover financial capacity",
        "annual turnover revenue financial",
        "company size financial strength",
    ],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    return text[start:end] if start >= 0 and end > start else text


def _call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
    try:
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )
        return response["message"]["content"] or ""
    except Exception as e:
        print(f"[TQExtractor] Ollama error: {e}")
        return ""


def _get_proposal_queries(parameter: str, criteria_text: str) -> List[str]:
    """Pick the most relevant RAG queries based on the parameter name."""
    param_lower = parameter.lower()
    combined = (param_lower + " " + criteria_text.lower())

    queries = []
    if any(kw in combined for kw in ["experience", "assignment", "eligible", "turnover", "capacity"]):
        queries.extend(PROPOSAL_QUERIES_BY_PATTERN["experience"])
        queries.extend(PROPOSAL_QUERIES_BY_PATTERN["turnover"])
    if any(kw in combined for kw in ["methodology", "work plan", "approach", "strategy"]):
        queries.extend(PROPOSAL_QUERIES_BY_PATTERN["methodology"])
    if any(kw in combined for kw in ["personnel", "team", "leader", "expert", "director", "staff"]):
        queries.extend(PROPOSAL_QUERIES_BY_PATTERN["personnel"])

    # Fallback: use parameter name directly
    if not queries:
        queries = [parameter, criteria_text[:80]]

    # Always add a direct parameter query
    queries.insert(0, parameter)
    return queries[:6]  # cap at 6 queries


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1: Extract scoring criteria from RFP
# ══════════════════════════════════════════════════════════════════════════════

def extract_scoring_criteria(rfp_doc_name: str, model: str = OLLAMA_MODEL) -> dict:
    """
    Uses RAG on the already-ingested RFP (in ChromaDB) to extract the
    technical evaluation scoring table.

    Args:
        rfp_doc_name: The filename used when ingesting the RFP (e.g. "abc123.pdf")
        model: Ollama model name

    Returns:
    {
        "evaluation_title": str,
        "grand_total_marks": int,
        "criteria": [...],
        "error": str or None,
        "raw_criteria_count": int,
    }
    """
    print(f"[TQExtractor] Extracting scoring criteria from: {rfp_doc_name}")

    # Multi-query retrieval
    seen_ids = set()
    all_chunks = []

    for query in SCORING_TABLE_QUERIES:
        chunks = retrieve(query, doc_name=rfp_doc_name, top_k=4)
        for chunk in chunks:
            cid = chunk["clause_ref"] + str(chunk["page_no"]) + str(len(chunk["text"]))
            if cid not in seen_ids and chunk["score"] > 0.20:
                seen_ids.add(cid)
                all_chunks.append(chunk)

    all_chunks.sort(key=lambda x: x["score"], reverse=True)
    all_chunks = all_chunks[:12]  # broader context for table extraction

    if not all_chunks:
        return {
            "evaluation_title": "Technical Evaluation",
            "grand_total_marks": 100,
            "criteria": [],
            "error": "No scoring table found in RFP document.",
            "raw_criteria_count": 0,
        }

    context = "\n\n---\n\n".join(
        f"[Page {c['page_no']} | {c['section_heading']}]\n{c['text']}"
        for c in all_chunks
    )

    prompt = CRITERIA_EXTRACTION_PROMPT.format(context=context)
    raw = _call_ollama(prompt, model=model)

    if not raw.strip():
        return {
            "evaluation_title": "Technical Evaluation",
            "grand_total_marks": 100,
            "criteria": [],
            "error": "LLM returned empty response.",
            "raw_criteria_count": 0,
        }

    try:
        parsed = json.loads(_clean_json(raw))
    except json.JSONDecodeError as e:
        print(f"[TQExtractor] JSON parse error on criteria: {e}")
        return {
            "evaluation_title": "Technical Evaluation",
            "grand_total_marks": 100,
            "criteria": [],
            "error": f"Could not parse LLM output: {e}",
            "raw_criteria_count": 0,
        }

    criteria = parsed.get("criteria", [])
    grand_total = parsed.get("grand_total_marks", 100)
    title = parsed.get("evaluation_title", "Technical Evaluation")

    # Flatten sub-items for scoring but keep structure
    flat_scoreable = _flatten_criteria(criteria)

    print(f"[TQExtractor] Extracted {len(criteria)} top-level criteria, "
          f"{len(flat_scoreable)} scoreable items. Grand total: {grand_total}")

    return {
        "evaluation_title": title,
        "grand_total_marks": grand_total,
        "criteria": criteria,
        "flat_scoreable": flat_scoreable,
        "error": None,
        "raw_criteria_count": len(flat_scoreable),
    }


def _flatten_criteria(criteria: list) -> list:
    """
    Flatten nested criteria into a list of scoreable items.
    If an item has sub_items, score the sub_items instead of the parent.
    """
    flat = []
    for item in criteria:
        sub_items = item.get("sub_items", [])
        if sub_items:
            # Score sub-items individually
            for sub in sub_items:
                flat.append({
                    "item_code":    sub.get("item_code", ""),
                    "parameter":    sub.get("parameter", ""),
                    "max_marks":    sub.get("max_marks", 0),
                    "criteria_text": sub.get("criteria_text") or item.get("criteria_text", ""),
                    "is_sub_item":  True,
                    "parent_parameter": item.get("parameter", ""),
                })
        else:
            flat.append({
                "item_code":    item.get("item_code", ""),
                "parameter":    item.get("parameter", ""),
                "max_marks":    item.get("max_marks", 0),
                "criteria_text": item.get("criteria_text", ""),
                "is_sub_item":  False,
                "parent_parameter": "",
            })
    return flat


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2: Ingest proposal + evaluate against each criterion
# ══════════════════════════════════════════════════════════════════════════════

def ingest_proposal(proposal_path: str, proposal_doc_name: str) -> int:
    """
    Parse and ingest a proposal document into ChromaDB under its own namespace.
    Returns number of chunks ingested.
    """
    print(f"[TQExtractor] Ingesting proposal: {proposal_path}")
    chunks = parse_document(proposal_path)
    # Tag all chunks with the proposal_doc_name for namespace isolation
    for chunk in chunks:
        chunk.doc_name = proposal_doc_name
        chunk.chunk_id = f"{proposal_doc_name}_{chunk.page_no}_{chunk.chunk_id.split('_')[-1]}"

    count = ingest_chunks(chunks, doc_id=proposal_doc_name)
    print(f"[TQExtractor] Proposal ingested: {count} chunks")
    return count


def evaluate_criterion_against_proposal(
    criterion: dict,
    proposal_doc_name: str,
    model: str = OLLAMA_MODEL,
) -> dict:
    """
    Evaluate a single scoring criterion against the proposal.

    Args:
        criterion: {item_code, parameter, max_marks, criteria_text, ...}
        proposal_doc_name: The doc_name used to store proposal chunks in ChromaDB
        model: Ollama model name

    Returns: {score, score_percentage, justification, strengths, gaps, evidence_found}
    """
    parameter    = criterion.get("parameter", "")
    max_marks    = criterion.get("max_marks", 0)
    criteria_text = criterion.get("criteria_text", "")

    if max_marks == 0:
        return {
            "score": 0, "score_percentage": 0,
            "justification": "Zero-mark criterion, skipped.",
            "strengths": [], "gaps": [], "evidence_found": False,
        }

    # Multi-query retrieval from proposal
    queries = _get_proposal_queries(parameter, criteria_text)
    seen_ids = set()
    all_chunks = []

    for query in queries:
        try:
            chunks = retrieve(query, doc_name=proposal_doc_name, top_k=3)
            for chunk in chunks:
                cid = str(chunk["page_no"]) + chunk["clause_ref"][:30]
                if cid not in seen_ids and chunk["score"] > 0.20:
                    seen_ids.add(cid)
                    all_chunks.append(chunk)
        except Exception:
            pass  # proposal may have very few chunks

    all_chunks.sort(key=lambda x: x["score"], reverse=True)
    all_chunks = all_chunks[:8]

    if not all_chunks:
        return {
            "score": 0,
            "score_percentage": 0,
            "justification": f"No relevant proposal content found for '{parameter}'.",
            "strengths": [],
            "gaps": [f"No content addressing '{parameter}' was found in the proposal."],
            "evidence_found": False,
        }

    proposal_context = "\n\n---\n\n".join(
        f"[Page {c['page_no']} | {c['section_heading']}]\n{c['text']}"
        for c in all_chunks
    )

    prompt = CRITERION_EVALUATION_PROMPT.format(
        parameter=parameter,
        max_marks=max_marks,
        criteria_text=criteria_text or "Score based on the quality of content provided.",
        proposal_context=proposal_context,
    )

    raw = _call_ollama(prompt, model=model)

    if not raw.strip():
        return {
            "score": 0, "score_percentage": 0,
            "justification": "LLM evaluation failed — manual review required.",
            "strengths": [], "gaps": ["Automated evaluation failed."], "evidence_found": False,
        }

    try:
        result = json.loads(_clean_json(raw))
        # Safety clamp
        score = max(0, min(float(result.get("score", 0)), max_marks))
        result["score"] = round(score, 1)
        result["score_percentage"] = round((score / max_marks) * 100, 1) if max_marks > 0 else 0
        return result
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[TQExtractor] Score parse error for '{parameter}': {e}")
        return {
            "score": 0, "score_percentage": 0,
            "justification": f"Could not parse evaluation response: {e}",
            "strengths": [], "gaps": ["Parse error — manual review required."],
            "evidence_found": False,
        }


def run_tq_evaluation(
    rfp_doc_name: str,
    proposal_path: str,
    proposal_doc_name: str,
    model: str = OLLAMA_MODEL,
    progress_callback=None,
) -> dict:
    """
    Full TQ evaluation pipeline.

    Args:
        rfp_doc_name:      The doc_name used when the RFP was ingested (e.g. "abc123.pdf")
        proposal_path:     Filesystem path to the uploaded proposal file
        proposal_doc_name: Unique name for this proposal in ChromaDB
        model:             Ollama model
        progress_callback: Optional callable(step: str, pct: int) for progress updates

    Returns:
    {
        "evaluation_title": str,
        "grand_total_marks": int,
        "total_scored": float,
        "total_percentage": float,
        "criteria_structure": [...],  # original nested structure for display
        "scores": [                   # flat scored items
            {
                "item_code": str,
                "parameter": str,
                "max_marks": int,
                "score": float,
                "score_percentage": float,
                "justification": str,
                "strengths": [...],
                "gaps": [...],
                "evidence_found": bool,
                "is_sub_item": bool,
                "parent_parameter": str,
            }
        ],
        "error": str or None,
    }
    """
    def _progress(step, pct):
        if progress_callback:
            progress_callback(step, pct)
        print(f"[TQ] {pct}% — {step}")

    _progress("Extracting scoring criteria from RFP", 10)
    criteria_result = extract_scoring_criteria(rfp_doc_name, model=model)

    if criteria_result.get("error") and not criteria_result.get("flat_scoreable"):
        return {
            "evaluation_title": "Technical Evaluation",
            "grand_total_marks": 100,
            "total_scored": 0,
            "total_percentage": 0,
            "criteria_structure": [],
            "scores": [],
            "error": criteria_result["error"],
        }

    flat_scoreable = criteria_result.get("flat_scoreable", [])
    grand_total = criteria_result.get("grand_total_marks", 100)

    _progress("Ingesting proposal document", 20)
    ingest_proposal(proposal_path, proposal_doc_name)

    _progress("Evaluating criteria against proposal", 30)

    scores = []
    n = len(flat_scoreable)
    for i, criterion in enumerate(flat_scoreable):
        step_pct = 30 + int((i / max(n, 1)) * 60)
        _progress(f"Evaluating: {criterion['parameter'][:50]}", step_pct)

        eval_result = evaluate_criterion_against_proposal(
            criterion, proposal_doc_name, model=model
        )

        scores.append({
            "item_code":        criterion["item_code"],
            "parameter":        criterion["parameter"],
            "max_marks":        criterion["max_marks"],
            "is_sub_item":      criterion["is_sub_item"],
            "parent_parameter": criterion.get("parent_parameter", ""),
            "criteria_text":    criterion.get("criteria_text", ""),
            **eval_result,
        })

        print(f"  [{i+1}/{n}] {criterion['parameter'][:40]} → "
              f"{eval_result['score']}/{criterion['max_marks']}")

    total_scored = sum(s["score"] for s in scores)
    total_percentage = round((total_scored / grand_total) * 100, 1) if grand_total > 0 else 0

    _progress("Finalising results", 95)

    return {
        "evaluation_title":  criteria_result.get("evaluation_title", "Technical Evaluation"),
        "grand_total_marks": grand_total,
        "total_scored":      round(total_scored, 1),
        "total_percentage":  total_percentage,
        "criteria_structure": criteria_result.get("criteria", []),
        "scores":            scores,
        "error":             criteria_result.get("error"),
    }
