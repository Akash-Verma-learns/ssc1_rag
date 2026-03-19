"""
Routes
------
All FastAPI endpoints:
  /auth/login, /auth/me
  /rfps, /rfps/upload, /rfps/{id}, /rfps/{id}/status, /rfps/{id}/download
  /rfps/{id}/comments
  /rfps/{id}/complete
  /users
"""

import uuid
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Query
from fastapi.responses import FileResponse
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


def rfp_to_dict(rfp: RFP, include_clauses: bool = False) -> dict:
    d = {
        "id": rfp.id,
        "opportunity_name": rfp.opportunity_name,
        "client_name": rfp.client_name,
        "bu": rfp.bu,
        "classification": rfp.classification,
        "state": rfp.state,
        "offering": rfp.offering,
        "solutions": rfp.solutions,
        "file_name": rfp.file_name,
        "job_id": rfp.job_id,
        "status": rfp.status,
        "progress": rfp.progress,
        "current_step": rfp.current_step,
        "error_message": rfp.error_message,
        "created_at": rfp.created_at.isoformat() if rfp.created_at else None,
        "uploaded_by_name": rfp.uploaded_by_user.name if rfp.uploaded_by_user else "",
    }
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

        # Run the pipeline — it returns results dict
        result = run_pipeline(
            rfp_path=file_path,
            output_path=output_path,
            model="llama3.2",  # uses default from extractor.py
        )

        update("processing", 80, "Saving results")

        # Save each clause result to DB
        CLAUSE_ORDER = [
            "liability", "insurance", "scope", "payment", "deliverables",
            "personnel", "ld", "penalties", "termination", "eligibility"
        ]

        for clause_type in CLAUSE_ORDER:
            clause_data = result.get("results", {}).get(clause_type, {})
            extracted = clause_data.get("extracted", {})

            # Get risk info from pipeline results
            risk_level = clause_data.get("risk_level", "NEEDS_REVIEW")
            risk_desc  = clause_data.get("risk_description", "")

            # Get auto_remark, needs_exception, needs_eqcr from risk object
            # pipeline result["results"] has flattened data
            auto_remark        = extracted.get("auto_remark", "")
            needs_exception    = extracted.get("needs_exception_approval", False)
            needs_eqcr         = extracted.get("needs_eqcr", False)
            deviation          = extracted.get("deviation_suggested", "")

            # Build clause ref string
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
# RFP ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/rfps")
def list_rfps(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfps = db.query(RFP).order_by(RFP.created_at.desc()).all()
    return [rfp_to_dict(r) for r in rfps]


@router.post("/rfps/upload")
def upload_rfp(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    opportunity_name: str = Form(...),
    client_name: str = Form(""),
    bu: str = Form(""),
    classification: str = Form("RFP/RFQ"),
    state: str = Form(""),
    offering: str = Form(""),
    solutions: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in (".pdf", ".docx", ".doc"):
        raise HTTPException(400, "Only PDF and DOCX files are supported.")

    job_id    = str(uuid.uuid4())[:8]
    save_path = UPLOAD_DIR / f"{job_id}{ext}"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    rfp = RFP(
        opportunity_name=opportunity_name,
        client_name=client_name,
        bu=bu,
        classification=classification,
        state=state,
        offering=offering,
        solutions=solutions,
        file_name=file.filename,
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
    return rfp_to_dict(rfp, include_clauses=True)


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

    # Allow delete if admin OR own comment
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
