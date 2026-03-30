"""
fix_rfp_data.py
---------------
Run this from your ssc1_rag folder to:
  1. See all RFPs in the database with their raw stored values
  2. Fix the opportunity_name for any RFP that accidentally got the filename stored

Usage:
    python fix_rfp_data.py              ← list all RFPs
    python fix_rfp_data.py --fix 6 "My Correct Opportunity Name" "Client Ltd"
                                        ← fix RFP id=6
"""

import sys
import os
import json

# Make sure we can import from the project
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal, RFP

db = SessionLocal()

def list_rfps():
    rfps = db.query(RFP).order_by(RFP.id).all()
    print(f"\n{'ID':<5} {'STATUS':<12} {'OPPORTUNITY NAME':<40} {'CLIENT':<20} {'RAW OFFERING':<30}")
    print("-" * 115)
    for r in rfps:
        print(f"{r.id:<5} {r.status:<12} {(r.opportunity_name or '')[:39]:<40} {(r.client_name or '')[:19]:<20} {(r.offering or '')[:29]:<30}")
    print()

def fix_rfp(rfp_id: int, opportunity_name: str, client_name: str = None):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        print(f"ERROR: RFP {rfp_id} not found")
        return
    
    print(f"Before: opportunity_name={rfp.opportunity_name!r}  client={rfp.client_name!r}")
    rfp.opportunity_name = opportunity_name
    if client_name:
        rfp.client_name = client_name
    db.commit()
    db.refresh(rfp)
    print(f"After:  opportunity_name={rfp.opportunity_name!r}  client={rfp.client_name!r}")
    print("Done.")

if __name__ == "__main__":
    args = sys.argv[1:]
    
    if not args or args[0] == "--list":
        list_rfps()
    elif args[0] == "--fix" and len(args) >= 3:
        rfp_id = int(args[1])
        opp_name = args[2]
        client = args[3] if len(args) >= 4 else None
        fix_rfp(rfp_id, opp_name, client)
        list_rfps()
    else:
        print(__doc__)

db.close()
