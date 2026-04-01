"""
Auth
----
Password hashing and JWT token creation/verification.
Roles: admin | reviewer | tq_reviewer
"""

import os
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET       = os.getenv("JWT_SECRET", "change_this_secret_key_123456")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

VALID_ROLES = {"admin", "reviewer", "tq_reviewer"}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


# ── FastAPI dependencies ───────────────────────────────────────────────────────

def get_current_user(token: str = Depends(oauth2_scheme)):
    """Inject into any route to require authentication."""
    from database import SessionLocal, User
    user_id = decode_token(token)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    finally:
        db.close()


def require_admin(current_user=Depends(get_current_user)):
    """Inject into any route to require admin role."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def require_pq_access(current_user=Depends(get_current_user)):
    """
    Require PQ (SSC1) access.
    Allowed: admin, reviewer
    NOT allowed: tq_reviewer (TQ-only team)
    """
    if current_user.role not in ("admin", "reviewer"):
        raise HTTPException(
            status_code=403,
            detail="PQ review access required. TQ reviewers cannot access the PQ section."
        )
    return current_user


def require_tq_access(current_user=Depends(get_current_user)):
    """
    Require TQ (SSC2) access.
    Allowed: admin, reviewer, tq_reviewer
    All authenticated users can access TQ.
    """
    if current_user.role not in ("admin", "reviewer", "tq_reviewer"):
        raise HTTPException(status_code=403, detail="Access denied")
    return current_user


def require_tq_or_admin(current_user=Depends(get_current_user)):
    """
    For TQ upload / trigger actions — admin and tq_reviewer only.
    Regular PQ reviewers cannot upload proposals.
    """
    if current_user.role not in ("admin", "tq_reviewer"):
        raise HTTPException(
            status_code=403,
            detail="TQ reviewer or admin access required to upload proposals."
        )
    return current_user