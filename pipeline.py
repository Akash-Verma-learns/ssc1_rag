"""
SSC1 PQ Automation Pipeline
----------------------------
Main orchestrator. Upload an RFP → get a filled SSC1 table.

Usage:
    python pipeline.py --rfp path/to/tender.pdf --output filled_ssc1.docx

Or programmatically:
    from pipeline import run_pipeline
    result = run_pipeline("tender.pdf", "output.docx")
"""

import argparse
import json
from pathlib import Path

from core.parser import parse_document
from core.vector_store import ingest_chunks
from core.extractor import extract_all_clauses, OLLAMA_MODEL
from rules.risk_engine import evaluate_clause, EVALUATORS
from output.writer import build_table_rows, fill_ssc1_table

TEMPLATE_PATH = "document_for_format.docx"   # the blank SSC1 template


# ──────────────────────────────────────────────────────────────────────────────
# Clause type → risk engine mapping
# ──────────────────────────────────────────────────────────────────────────────

CLAUSE_TO_RISK_KEY = {
    "liability":    "liability",
    "insurance":    "insurance",
    "scope":        "scope",
    "payment":      "payment",
    "deliverables": "deliverables",
    "personnel":    "personnel",
    "ld":           "ld",
    "penalties":    "penalties",
    "termination":  "termination",
    "eligibility":  "eligibility",
}


def run_pipeline(
    rfp_path: str,
    output_path: str,
    template_path: str = TEMPLATE_PATH,
    model: str = OLLAMA_MODEL,
    skip_ingest: bool = False,
) -> dict:
    """
    Full pipeline: parse → ingest → extract → evaluate → write.

    Args:
        rfp_path:      Path to the RFP/tender PDF or DOCX
        output_path:   Where to save the filled SSC1 DOCX
        template_path: Path to blank SSC1 template DOCX
        model:         Ollama model name
        skip_ingest:   Set True if document was already ingested (reuse vector store)

    Returns:
        Summary dict with results for each clause
    """
    rfp_name = Path(rfp_path).stem
    doc_name = Path(rfp_path).name

    print(f"\n{'='*60}")
    print(f"SSC1 PIPELINE: {rfp_name}")
    print(f"{'='*60}")

    # ── Step 1: Parse ──────────────────────────────────────────────────────────
    if not skip_ingest:
        print(f"\n[1/4] Parsing document: {rfp_path}")
        chunks = parse_document(rfp_path)
        print(f"      → {len(chunks)} chunks extracted")

        # ── Step 2: Ingest into vector store ──────────────────────────────────
        print(f"\n[2/4] Ingesting into ChromaDB...")
        count = ingest_chunks(chunks, doc_id=doc_name)
        print(f"      → {count} chunks ingested")
    else:
        print(f"\n[1-2/4] Skipping ingestion (skip_ingest=True)")

    # ── Step 3: Extract all clauses via RAG + LLM ─────────────────────────────
    print(f"\n[3/4] Extracting clauses with model '{model}'...")
    extraction_results = extract_all_clauses(doc_name, model=model)

    # ── Step 4a: Evaluate risk for each clause ─────────────────────────────────
    print(f"\n[4/4] Evaluating risk...")
    pipeline_results = {}

    for clause_type, ext_result in extraction_results.items():
        risk_key = CLAUSE_TO_RISK_KEY.get(clause_type)
        if not risk_key:
            continue

        extracted_data = ext_result.get("extracted", {})
        try:
            risk_result = evaluate_clause(risk_key, extracted_data)
        except Exception as e:
            from rules.risk_engine import RiskResult
            risk_result = RiskResult(
                clause_name=clause_type,
                risk_level="NEEDS_REVIEW",
                risk_description=f"Risk evaluation failed: {e}",
                auto_remark="",
            )

        pipeline_results[clause_type] = {
            **ext_result,
            "risk": risk_result,
        }

        level = risk_result.risk_level
        icon = {"HIGH": "🔴", "MEDIUM": "🟡", "ACCEPTABLE": "🟢", "LOW": "🟢", "NEEDS_REVIEW": "🔵"}.get(level, "⚪")
        print(f"  {icon} {clause_type:15s} → {level}")

    # ── Step 4b: Write DOCX output ────────────────────────────────────────────
    table_rows = build_table_rows(pipeline_results)
    fill_ssc1_table(
        rows=table_rows,
        template_path=template_path,
        output_path=output_path,
        rfp_name=rfp_name,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    high_risk = [k for k, v in pipeline_results.items() if v.get("risk") and v["risk"].risk_level == "HIGH"]
    medium_risk = [k for k, v in pipeline_results.items() if v.get("risk") and v["risk"].risk_level == "MEDIUM"]

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  High Risk clauses   : {len(high_risk)} — {', '.join(high_risk) or 'None'}")
    print(f"  Medium Risk clauses : {len(medium_risk)} — {', '.join(medium_risk) or 'None'}")
    print(f"  Output saved to     : {output_path}")
    print(f"{'='*60}\n")

    return {
        "rfp_name": rfp_name,
        "output_path": output_path,
        "high_risk": high_risk,
        "medium_risk": medium_risk,
        "results": {
            k: {
                "extracted": v.get("extracted", {}),
                "risk_level": v["risk"].risk_level if v.get("risk") else "N/A",
                "risk_description": v["risk"].risk_description if v.get("risk") else "",
            }
            for k, v in pipeline_results.items()
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SSC1 PQ Risk Review Automation")
    parser.add_argument("--rfp", required=True, help="Path to RFP/tender PDF or DOCX")
    parser.add_argument("--output", default="filled_ssc1.docx", help="Output DOCX path")
    parser.add_argument("--template", default=TEMPLATE_PATH, help="SSC1 template DOCX path")
    parser.add_argument("--model", default=OLLAMA_MODEL, help="Ollama model name")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip parsing+ingestion (reuse existing vector store)")

    args = parser.parse_args()

    result = run_pipeline(
        rfp_path=args.rfp,
        output_path=args.output,
        template_path=args.template,
        model=args.model,
        skip_ingest=args.skip_ingest,
    )

    print(json.dumps({
        "high_risk": result["high_risk"],
        "medium_risk": result["medium_risk"],
        "output": result["output_path"],
    }, indent=2))
