"""
Routes
------
All FastAPI endpoints:
  /auth/login, /auth/me
  /rfps, /rfps/upload, /rfps/{id}, /rfps/{id}/status, /rfps/{id}/download
  /rfps/{id}/comments
  /rfps/{id}/complete
  /users
  /offering-solutions  ← new: returns the offering→solutions mapping

Changes from v1:
  - classification field now stores BU code: TRF | ERC | DEALS | TAX
  - offering + solutions stored as JSON arrays (up to 5 pairs each)
  - uploaded_by_name hidden from non-admin users in list view
  - opportunity_name and client_name always returned (never omitted)
"""

import uuid
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, User, RFP, ClauseResult, Comment
from auth import (
    verify_password, create_token, hash_password,
    get_current_user, require_admin
)

router = APIRouter()

UPLOAD_DIR = Path("./uploads")
OUTPUT_DIR = Path("./outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Load offering→solutions map at startup
_OFFERING_SOLUTIONS_PATH = Path(__file__).parent / "offering_solutions.json"
try:
    with open(_OFFERING_SOLUTIONS_PATH, "r", encoding="utf-8") as f:
        OFFERING_SOLUTIONS: dict = json.load(f)
except FileNotFoundError:
    OFFERING_SOLUTIONS = {}


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

class CommentRequest(BaseModel):
    clause_type: str
    comment_text: str

class CreateUserRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "reviewer"


# ── Helpers ────────────────────────────────────────────────────────────────────

def user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def clause_to_dict(c: ClauseResult) -> dict:
    return {
        "clause_text": c.clause_text,
        "clause_reference": c.clause_reference,
        "page_no": c.page_no,
        "risk_level": c.risk_level,
        "risk_description": c.risk_description,
        "auto_remark": c.auto_remark,
        "needs_exception_approval": c.needs_exception,
        "needs_eqcr": c.needs_eqcr,
        "deviation_suggested": c.deviation_suggested,
    }


def _parse_offering_solutions(offering_str: str, solutions_str: str):
    """
    Parse offering and solutions fields. Always returns two plain lists.
    Handles:
      - New format: JSON array  '["ENERGY & RENEWABLES", "URBAN INFRA"]'
      - Old format: plain string 'ENERGY & RENEWABLES'
      - Empty / None
    """
    def _parse_one(raw):
        if not raw:
            return []
        raw = str(raw).strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if x and str(x).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        # Plain string fallback
        return [raw]

    return _parse_one(offering_str), _parse_one(solutions_str)


def rfp_to_dict(rfp: RFP, include_clauses: bool = False, viewer_role: str = "reviewer") -> dict:
    """
    Serialize an RFP to a dict.
    - opportunity_name and client_name are always included (even if empty — shown as "" not null)
    - uploaded_by_name is only included for admin viewers
    - offering/solutions are returned as parsed arrays
    """
    offerings, solutions = _parse_offering_solutions(rfp.offering or "", rfp.solutions or "")

    d = {
        "id": rfp.id,
        # Always return these even if blank; frontend shows "—" for empty
        "opportunity_name": rfp.opportunity_name or "",
        "client_name": rfp.client_name or "",
        "bu": rfp.bu or "",
        # classification now stores BU code: TRF | ERC | DEALS | TAX
        "bu_code": rfp.classification or "",
        "state": rfp.state or "",
        "country": rfp.country or "",
        # Multi-offering support: arrays of up to 5
        "offerings": offerings,
        "solutions": solutions,
        # Legacy single-value fields — old Lovable frontend reads these
        # If DB has plain string (old upload), return it directly; else first element of array
        "offering":  offerings[0] if offerings else (rfp.offering or ""),
        "solution":  solutions[0] if solutions else (rfp.solutions or ""),
        # Also expose raw DB strings so frontend can display them
        "offering_raw":  rfp.offering or "",
        "solutions_raw": rfp.solutions or "",
        "file_name": rfp.file_name or "",
        "job_id": rfp.job_id or "",
        "status": rfp.status or "queued",
        "progress": rfp.progress or 0,
        "current_step": rfp.current_step or "",
        "error_message": rfp.error_message,
        "created_at": rfp.created_at.isoformat() if rfp.created_at else None,
    }

    # Requestor name: admins only
    if viewer_role == "admin":
        d["uploaded_by_name"] = rfp.uploaded_by_user.name if rfp.uploaded_by_user else ""
    else:
        d["uploaded_by_name"] = None  # hidden

    if include_clauses:
        clauses = {}
        for c in rfp.clause_results:
            clauses[c.clause_type] = clause_to_dict(c)
        d["clauses"] = clauses

    return d


# ── Background task: run the full pipeline ─────────────────────────────────────

def run_pipeline_task(rfp_id: int, file_path: str, job_id: str):
    """
    Runs in background after upload.
    Updates RFP status + progress in DB as it goes.
    Saves clause results to DB when done.
    """
    from database import SessionLocal
    from pipeline import run_pipeline

    db = SessionLocal()
    try:
        rfp = db.query(RFP).filter(RFP.id == rfp_id).first()

        def update(status: str, progress: int, step: str):
            rfp.status = status
            rfp.progress = progress
            rfp.current_step = step
            db.commit()

        update("processing", 10, "Parsing document")

        output_path = str(OUTPUT_DIR / f"{job_id}_ssc1.docx")

        result = run_pipeline(
            rfp_path=file_path,
            output_path=output_path,
            model="llama3.2",
        )

        update("processing", 75, "Extracting document metadata")

        # ── Auto-extract opportunity_name and client_name if blank ────────────
        try:
            from core.metadata_extractor import extract_metadata
            from pathlib import Path as _Path
            doc_name = _Path(file_path).name
            meta = extract_metadata(doc_name)

            # Only fill in if the user left them blank at upload time
            if not rfp.opportunity_name and meta.get("opportunity_name"):
                rfp.opportunity_name = meta["opportunity_name"]
                print(f"[Pipeline] Auto-filled opportunity_name: {rfp.opportunity_name!r}")

            if not rfp.client_name and meta.get("client_name"):
                rfp.client_name = meta["client_name"]
                print(f"[Pipeline] Auto-filled client_name: {rfp.client_name!r}")

            db.commit()
        except Exception as meta_err:
            print(f"[Pipeline] Metadata extraction skipped: {meta_err}")

        update("processing", 80, "Saving results")

        CLAUSE_ORDER = [
            "liability", "insurance", "scope", "payment", "deliverables",
            "personnel", "ld", "penalties", "termination", "eligibility"
        ]

        for clause_type in CLAUSE_ORDER:
            clause_data = result.get("results", {}).get(clause_type, {})
            extracted = clause_data.get("extracted", {})

            risk_level = clause_data.get("risk_level", "NEEDS_REVIEW")
            risk_desc  = clause_data.get("risk_description", "")

            auto_remark        = extracted.get("auto_remark", "")
            needs_exception    = extracted.get("needs_exception_approval", False)
            needs_eqcr         = extracted.get("needs_eqcr", False)
            deviation          = extracted.get("deviation_suggested", "")

            clause_ref = extracted.get("clause_reference", "")
            page_no    = str(extracted.get("page_no", "") or "")

            clause_result = ClauseResult(
                rfp_id               = rfp_id,
                clause_type          = clause_type,
                clause_text          = extracted.get("clause_text") or extracted.get("summary", ""),
                clause_reference     = clause_ref,
                page_no              = page_no,
                risk_level           = risk_level,
                risk_description     = risk_desc,
                auto_remark          = auto_remark,
                needs_exception      = bool(needs_exception),
                needs_eqcr           = bool(needs_eqcr),
                deviation_suggested  = deviation or "",
            )
            db.add(clause_result)

        rfp.status = "completed"
        rfp.progress = 100
        rfp.current_step = "Done"
        db.commit()
        print(f"[Pipeline] RFP {rfp_id} completed.")

    except Exception as e:
        db.query(RFP).filter(RFP.id == rfp_id).update({
            "status": "failed",
            "error_message": str(e),
            "current_step": "Failed",
        })
        db.commit()
        print(f"[Pipeline] RFP {rfp_id} FAILED: {e}")
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user_to_dict(user),
    }


@router.get("/auth/me")
def me(current_user: User = Depends(get_current_user)):
    return user_to_dict(current_user)


# ══════════════════════════════════════════════════════════════════════════════
# OFFERING-SOLUTIONS MAP
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/offering-solutions")
def get_offering_solutions(current_user: User = Depends(get_current_user)):
    """
    Returns the full offering → solutions mapping.
    Frontend uses this to populate the linked dropdowns in the upload modal.
    """
    return OFFERING_SOLUTIONS


# ══════════════════════════════════════════════════════════════════════════════
# RFP ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/rfps")
def list_rfps(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfps = db.query(RFP).order_by(RFP.created_at.desc()).all()
    return [rfp_to_dict(r, viewer_role=current_user.role) for r in rfps]


@router.post("/rfps/upload")
async def upload_rfp(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Upload an RFP. Uses raw form parsing so it works regardless of which
    field names the frontend sends. Logs all received fields for debugging.
    """
    # Parse multipart form — captures ALL fields whatever their names are
    form = await request.form()
    
    # Log every field received (visible in your uvicorn terminal)
    print("\n[UPLOAD DEBUG] Fields received from frontend:")
    for key in form.keys():
        val = form[key]
        if hasattr(val, 'filename'):
            print(f"  FILE  {key!r} = {val.filename!r} ({val.content_type})")
        else:
            print(f"  FIELD {key!r} = {str(val)!r}")
    print()

    # ── File ─────────────────────────────────────────────────────────────────
    # Try common file field names
    file_obj = form.get("file") or form.get("rfp_file") or form.get("document")
    if file_obj is None:
        # Take first UploadFile found regardless of name
        for key in form.keys():
            v = form[key]
            if hasattr(v, 'filename') and v.filename:
                file_obj = v
                break
    if file_obj is None:
        raise HTTPException(400, "No file found in upload. Expected a field named 'file'.")

    ext = Path(file_obj.filename).suffix.lower()
    if ext not in (".pdf", ".docx", ".doc"):
        raise HTTPException(400, f"Only PDF and DOCX files are supported. Got: {ext!r}")

    # Build a case-insensitive lookup of ALL form fields
    # This catches any field name Lovable sends regardless of casing
    form_lower = {}
    for k in form.keys():
        v = form.get(k)
        if not hasattr(v, "filename"):  # skip file fields
            form_lower[k.lower().replace(" ", "_").replace("-", "_")] = str(v).strip()

    print("[UPLOAD DEBUG] Normalised field map:")
    for k, v in sorted(form_lower.items()):
        print(f"  {k!r} = {v!r}")
    print()

    def _f(*names, default=""):
        """Look up field by any of the given name variants (case-insensitive)."""
        for name in names:
            # Try exact name first
            v = form.get(name)
            if v is not None and not hasattr(v, "filename"):
                val = str(v).strip()
                if val:
                    return val
            # Try normalised lookup
            norm = name.lower().replace(" ", "_").replace("-", "_")
            if norm in form_lower and form_lower[norm]:
                return form_lower[norm]
        return default

    # ── Text fields — every possible variant Lovable might use ───────────────
    opportunity_name = _f(
        "opportunity_name", "opportunityName", "Opportunity Name",
        "opportunityname", "opportunity", "rfp_name", "rfpName", "title"
    )
    client_name = _f(
        "client_name", "clientName", "Client Name",
        "clientname", "client", "client_organisation", "clientOrganisation"
    )
    bu = _f(
        "bu", "name_of_bu", "nameOfBu", "Name of BU", "nameof_bu",
        "business_unit", "businessUnit", "buName", "bu_name", "practice"
    )
    state = _f(
        "state", "State", "indian_state", "indianState", "location"
    )
    country = _f(
        "country", "Country", "nation"
    )
    bu_code = _f(
        "bu_code", "buCode", "Bu Code", "bu_code", "BU Code",
        "classification", "Classification", "bunit", "bCode"
    ) or "TRF"

    # ── Offering / Solutions ──────────────────────────────────────────────────
    def _parse_array(raw):
        """Turn a raw form value into a list. Handles JSON arrays and plain strings."""
        if not raw:
            return []
        raw = str(raw).strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                return [str(x).strip() for x in parsed if x and str(x).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        return [raw] if raw else []

    # JSON array format (new frontend) — try all variants
    raw_offerings = _f(
        "offerings_json", "offeringsJson", "offerings",
        "Offerings", "offering_list", "offeringList"
    )
    raw_solutions = _f(
        "solutions_json", "solutionsJson", "solutions",
        "Solutions", "solution_list", "solutionList", "solution"
    )

    resolved_offerings = _parse_array(raw_offerings)
    resolved_solutions  = _parse_array(raw_solutions)

    # Individual row fields: offering_1, offering_2 ... offering_5 (some Lovable builds)
    for i in range(1, 6):
        o = _f(f"offering_{i}", f"offering{i}", f"Offering {i}")
        s = _f(f"solution_{i}", f"solution{i}", f"Solution {i}")
        if o and o not in resolved_offerings:
            resolved_offerings.append(o)
        if s and s not in resolved_solutions:
            resolved_solutions.append(s)

    resolved_offerings = [x for x in resolved_offerings if x][:5]
    resolved_solutions  = [x for x in resolved_solutions  if x][:5]

    print(f"[UPLOAD PARSED] opportunity_name={opportunity_name!r}")
    print(f"[UPLOAD PARSED] client_name={client_name!r}  bu={bu!r}  bu_code={bu_code!r}  state={state!r}")
    print(f"[UPLOAD PARSED] offerings={resolved_offerings}")
    print(f"[UPLOAD PARSED] solutions={resolved_solutions}")

    # ── Save file to disk ─────────────────────────────────────────────────────
    job_id    = str(uuid.uuid4())[:8]
    save_path = UPLOAD_DIR / f"{job_id}{ext}"
    contents  = await file_obj.read()
    with open(save_path, "wb") as fp:
        fp.write(contents)

    # ── Save RFP record to DB ─────────────────────────────────────────────────
    rfp = RFP(
        # Leave blank if user didn't fill it — pipeline will auto-extract from document
        opportunity_name=opportunity_name or "",
        client_name=client_name,
        bu=bu,
        classification=bu_code,
        state=state,
        country=country,
        offering=json.dumps(resolved_offerings, ensure_ascii=False),
        solutions=json.dumps(resolved_solutions, ensure_ascii=False),
        file_name=file_obj.filename,
        job_id=job_id,
        status="queued",
        progress=0,
        uploaded_by=current_user.id,
    )
    db.add(rfp)
    db.commit()
    db.refresh(rfp)

    background_tasks.add_task(run_pipeline_task, rfp.id, str(save_path), job_id)

    return {"job_id": job_id, "rfp_id": rfp.id, "status": "queued"}


@router.get("/rfps/{rfp_id}/status")
def get_status(
    rfp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")
    return {
        "rfp_id": rfp.id,
        "status": rfp.status,
        "progress": rfp.progress,
        "current_step": rfp.current_step,
        "error_message": rfp.error_message,
    }


@router.get("/rfps/{rfp_id}")
def get_rfp(
    rfp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")
    return rfp_to_dict(rfp, include_clauses=True, viewer_role=current_user.role)


@router.post("/rfps/{rfp_id}/complete")
def mark_complete(
    rfp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")
    rfp.status = "completed"
    db.commit()
    return {"status": "completed"}

@router.patch("/rfps/{rfp_id}")
async def update_rfp(
    rfp_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Update editable metadata fields on an existing RFP.
    Accepts JSON body with any subset of: opportunity_name, client_name,
    bu, bu_code, state, offering, solution, offerings_json, solutions_json
    """
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be valid JSON")

    def _parse_arr(raw):
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if x and str(x).strip()]
        raw = str(raw).strip()
        if raw.startswith("["):
            try:
                return [str(x).strip() for x in json.loads(raw) if x]
            except Exception:
                pass
        return [raw] if raw else []

    if "opportunity_name" in body:
        rfp.opportunity_name = str(body["opportunity_name"]).strip()
    if "client_name" in body:
        rfp.client_name = str(body["client_name"]).strip()
    if "bu" in body:
        rfp.bu = str(body["bu"]).strip()
    if "bu_code" in body or "classification" in body:
        rfp.classification = str(body.get("bu_code") or body.get("classification", "")).strip()
    if "state" in body:
        rfp.state = str(body["state"]).strip()
    if "country" in body:
        rfp.country = str(body["country"]).strip()

    # Offerings / solutions update
    new_offerings = None
    new_solutions  = None

    if "offerings_json" in body:
        new_offerings = _parse_arr(body["offerings_json"])
    elif "offerings" in body:
        new_offerings = _parse_arr(body["offerings"])
    elif "offering" in body:
        new_offerings = _parse_arr(body["offering"])

    if "solutions_json" in body:
        new_solutions = _parse_arr(body["solutions_json"])
    elif "solutions" in body:
        new_solutions = _parse_arr(body["solutions"])
    elif "solution" in body:
        new_solutions = _parse_arr(body["solution"])

    if new_offerings is not None:
        rfp.offering = json.dumps(new_offerings[:5], ensure_ascii=False)
    if new_solutions is not None:
        rfp.solutions = json.dumps(new_solutions[:5], ensure_ascii=False)

    db.commit()
    db.refresh(rfp)
    return rfp_to_dict(rfp, include_clauses=False, viewer_role=current_user.role)



@router.get("/rfps/{rfp_id}/download")
def download_rfp(
    rfp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")

    output_path = OUTPUT_DIR / f"{rfp.job_id}_ssc1.docx"
    if not output_path.exists():
        raise HTTPException(404, "Output file not ready yet. Run analysis first.")

    return FileResponse(
        path=str(output_path),
        filename=f"SSC1_Review_{rfp.opportunity_name[:30]}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ══════════════════════════════════════════════════════════════════════════════
# COMMENTS ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/rfps/{rfp_id}/comments")
def get_comments(
    rfp_id: int,
    clause: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Comment).filter(Comment.rfp_id == rfp_id)
    if clause:
        q = q.filter(Comment.clause_type == clause)
    comments = q.order_by(Comment.created_at.asc()).all()
    return [
        {
            "id": c.id,
            "clause_type": c.clause_type,
            "user_id": c.user_id,
            "user_name": c.user.name if c.user else "Unknown",
            "user_role": c.user.role if c.user else "",
            "comment_text": c.comment_text,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]


@router.post("/rfps/{rfp_id}/comments")
def post_comment(
    rfp_id: int,
    body: CommentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfp = db.query(RFP).filter(RFP.id == rfp_id).first()
    if not rfp:
        raise HTTPException(404, "RFP not found")

    comment = Comment(
        rfp_id=rfp_id,
        clause_type=body.clause_type,
        user_id=current_user.id,
        comment_text=body.comment_text,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    return {
        "id": comment.id,
        "clause_type": comment.clause_type,
        "user_id": comment.user_id,
        "user_name": current_user.name,
        "user_role": current_user.role,
        "comment_text": comment.comment_text,
        "created_at": comment.created_at.isoformat(),
    }


@router.delete("/rfps/{rfp_id}/comments/{comment_id}")
def delete_comment(
    rfp_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = db.query(Comment).filter(
        Comment.id == comment_id,
        Comment.rfp_id == rfp_id
    ).first()
    if not comment:
        raise HTTPException(404, "Comment not found")

    if current_user.role != "admin" and comment.user_id != current_user.id:
        raise HTTPException(403, "Not allowed to delete this comment")

    db.delete(comment)
    db.commit()
    return {"deleted": True}


# ══════════════════════════════════════════════════════════════════════════════
# USERS ROUTES (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return [user_to_dict(u) for u in db.query(User).all()]


@router.post("/users")
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, "Email already registered")
    if body.role not in ("admin", "reviewer"):
        raise HTTPException(400, "Role must be 'admin' or 'reviewer'")

    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_to_dict(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if user_id == current_user.id:
        raise HTTPException(400, "Cannot delete yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.delete(user)
    db.commit()
    return {"deleted": True}