"""
pipeline_fix.py
---------------
Run this once to patch routes.py in place.
It moves metadata extraction to run immediately after ingestion,
BEFORE extract_all_clauses, so it always works regardless of
whether the advanced learning features are available.

Usage:
    python pipeline_fix.py
"""

import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

routes_path = "routes.py"

with open(routes_path, "r", encoding="utf-8") as f:
    content = f.read()

# ── Find the ingest_chunks line and insert metadata extraction after it ───────
# We look for the pattern: ingest_chunks(...) then the next update(...) call

old = '''        update("processing", 35, "Ingesting into vector store")
        ingest_chunks(chunks, doc_id=doc_name)

        update("processing", 50, "Extracting clauses (with learning context)")'''

new = '''        update("processing", 35, "Ingesting into vector store")
        ingest_chunks(chunks, doc_id=doc_name)

        # Metadata extraction — runs immediately after ingestion so it always
        # completes even if the clause extraction fails for any reason.
        update("processing", 45, "Extracting document metadata")
        try:
            from core.metadata_extractor import extract_metadata
            meta = extract_metadata(doc_name)
            if not rfp.opportunity_name and meta.get("opportunity_name"):
                rfp.opportunity_name = meta["opportunity_name"]
                print(f"[Pipeline] Auto-filled opportunity_name: {rfp.opportunity_name!r}")
            if not rfp.client_name and meta.get("client_name"):
                rfp.client_name = meta["client_name"]
                print(f"[Pipeline] Auto-filled client_name: {rfp.client_name!r}")
            db.commit()
        except Exception as meta_err:
            print(f"[Pipeline] Metadata extraction skipped: {meta_err}")

        update("processing", 50, "Extracting clauses (with learning context)")'''

if old in content:
    content = content.replace(old, new)
    print("SUCCESS: Metadata extraction moved to after ingestion")
else:
    print("Pattern not found exactly. Trying fallback search...")
    # Try to find ingest_chunks line and insert after it
    idx = content.find('ingest_chunks(chunks, doc_id=doc_name)')
    if idx == -1:
        print("ERROR: Could not find ingest_chunks line. Please manually add the metadata block.")
        sys.exit(1)
    
    # Find the next update() call after ingest_chunks
    next_update = content.find('update("processing", 50', idx)
    if next_update == -1:
        next_update = content.find('update("processing"', idx + 50)
    
    if next_update == -1:
        print("ERROR: Could not find next update() call")
        sys.exit(1)
    
    insert_block = '''
        # Metadata extraction — runs immediately after ingestion
        update("processing", 45, "Extracting document metadata")
        try:
            from core.metadata_extractor import extract_metadata
            meta = extract_metadata(doc_name)
            if not rfp.opportunity_name and meta.get("opportunity_name"):
                rfp.opportunity_name = meta["opportunity_name"]
                print(f"[Pipeline] Auto-filled opportunity_name: {rfp.opportunity_name!r}")
            if not rfp.client_name and meta.get("client_name"):
                rfp.client_name = meta["client_name"]
                print(f"[Pipeline] Auto-filled client_name: {rfp.client_name!r}")
            db.commit()
        except Exception as meta_err:
            print(f"[Pipeline] Metadata extraction skipped: {meta_err}")

'''
    content = content[:next_update] + insert_block + content[next_update:]
    print("SUCCESS: Metadata block inserted via fallback method")

# ── Remove the duplicate metadata block that runs later ──────────────────────
old2 = '''        update("processing", 75, "Extracting document metadata")

        try:
            from core.metadata_extractor import extract_metadata
            meta = extract_metadata(doc_name)
            if not rfp.opportunity_name and meta.get("opportunity_name"):
                rfp.opportunity_name = meta["opportunity_name"]
            if not rfp.client_name and meta.get("client_name"):
                rfp.client_name = meta["client_name"]
            db.commit()
        except Exception as meta_err:
            print(f"[Pipeline] Metadata extraction skipped: {meta_err}")

        update("processing", 82, "Saving results")'''

if old2 in content:
    content = content.replace(old2, '        update("processing", 82, "Saving results")')
    print("Removed duplicate metadata block")

with open(routes_path, "w", encoding="utf-8") as f:
    f.write(content)

print("\nDone. Restart the server: python -m uvicorn api:app --reload --port 8000")
