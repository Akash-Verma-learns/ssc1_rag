"""
backfill_metadata.py
---------------------
Re-runs metadata extraction (opportunity_name + client_name) on all
completed RFPs that currently have blank values for these fields.

Requires:
  - Ollama running: ollama serve
  - Documents still present in ChromaDB (they are -- ChromaDB is persistent)

Usage:
    python backfill_metadata.py           <- dry run (shows what would change)
    python backfill_metadata.py --apply   <- actually saves changes to DB
"""

import sys, os
# Fix: use abspath so the path works regardless of how the script is invoked
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal, RFP
from pathlib import Path
from core.metadata_extractor import extract_metadata

APPLY = "--apply" in sys.argv

db = SessionLocal()
rfps = db.query(RFP).filter(RFP.status == "completed").order_by(RFP.id).all()

print(f"\nFound {len(rfps)} completed RFPs\n")

updated = 0
for rfp in rfps:
    needs_opp    = not rfp.opportunity_name or rfp.opportunity_name.strip() == ""
    needs_client = not rfp.client_name      or rfp.client_name.strip()      == ""

    if not needs_opp and not needs_client:
        print(f"  RFP {rfp.id}: OK -- {rfp.opportunity_name!r} / {rfp.client_name!r}")
        continue

    # ChromaDB stores chunks under the job_id filename (e.g. "7f6ec075.pdf")
    # NOT the original filename — must reconstruct from job_id
    if not rfp.job_id:
        print(f"  RFP {rfp.id}: SKIP -- no job_id in DB")
        continue
    original_ext = Path(rfp.file_name).suffix if rfp.file_name else ".pdf"
    doc_name = f"{rfp.job_id}{original_ext}"

    print(f"\n  RFP {rfp.id}: extracting metadata from {doc_name!r}...")
    meta = extract_metadata(doc_name)

    opp_new    = meta.get("opportunity_name")
    client_new = meta.get("client_name")
    err        = meta.get("error")

    if err:
        print(f"    ERROR: {err}")
        continue

    print(f"    opportunity_name -> {opp_new!r}")
    print(f"    client_name      -> {client_new!r}")

    if APPLY:
        if needs_opp    and opp_new:    rfp.opportunity_name = opp_new
        if needs_client and client_new: rfp.client_name      = client_new
        db.commit()
        print(f"    SAVED.")
        updated += 1
    else:
        print(f"    (dry run -- use --apply to save)")

db.close()

if APPLY:
    print(f"\nUpdated {updated} RFP records. Restart server to see changes.")
else:
    print(f"\nDry run complete. Run with --apply to save changes.")