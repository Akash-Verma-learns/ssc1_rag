"""
Database
--------
SQLAlchemy models + Postgres connection.
Tables are auto-created on first run.
"""

import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text,
    Boolean, DateTime, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env file")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# ── Dependency for FastAPI routes ──────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Tables ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(100), nullable=False)
    email         = Column(String(150), unique=True, index=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    role          = Column(Enum("admin", "reviewer", name="user_role"), default="reviewer")
    created_at    = Column(DateTime, default=datetime.utcnow)

    rfps          = relationship("RFP", back_populates="uploaded_by_user")
    comments      = relationship("Comment", back_populates="user")


class RFP(Base):
    __tablename__ = "rfps"

    id               = Column(Integer, primary_key=True, index=True)
    opportunity_name = Column(String(300), nullable=False)
    client_name      = Column(String(150))
    bu               = Column(String(150))
    classification   = Column(String(50))
    state            = Column(String(100))
    offering         = Column(String(150))
    solutions        = Column(String(150))
    file_name        = Column(String(300))
    job_id           = Column(String(50), unique=True, index=True)
    status           = Column(String(30), default="queued")   # queued / processing / completed / failed
    progress         = Column(Integer, default=0)
    current_step     = Column(String(100), default="")
    error_message    = Column(Text, nullable=True)
    uploaded_by      = Column(Integer, ForeignKey("users.id"))
    created_at       = Column(DateTime, default=datetime.utcnow)

    uploaded_by_user = relationship("User", back_populates="rfps")
    clause_results   = relationship("ClauseResult", back_populates="rfp", cascade="all, delete")
    comments         = relationship("Comment", back_populates="rfp", cascade="all, delete")


class ClauseResult(Base):
    __tablename__ = "clause_results"

    id                    = Column(Integer, primary_key=True, index=True)
    rfp_id                = Column(Integer, ForeignKey("rfps.id"), nullable=False)
    clause_type           = Column(String(50))    # liability, insurance, scope, etc.
    clause_text           = Column(Text)
    clause_reference      = Column(String(200))
    page_no               = Column(String(20))
    risk_level            = Column(String(30))    # HIGH / MEDIUM / ACCEPTABLE / LOW / NEEDS_REVIEW
    risk_description      = Column(Text)
    auto_remark           = Column(Text)
    needs_exception       = Column(Boolean, default=False)
    needs_eqcr            = Column(Boolean, default=False)
    deviation_suggested   = Column(Text)

    rfp = relationship("RFP", back_populates="clause_results")


class Comment(Base):
    __tablename__ = "comments"

    id           = Column(Integer, primary_key=True, index=True)
    rfp_id       = Column(Integer, ForeignKey("rfps.id"), nullable=False)
    clause_type  = Column(String(50))
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    comment_text = Column(Text, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)

    rfp  = relationship("RFP", back_populates="comments")
    user = relationship("User", back_populates="comments")


# ── Create all tables + seed admin user ───────────────────────────────────────

def init_db():
    """Create tables and seed default admin user if not exists."""
    Base.metadata.create_all(bind=engine)

    from auth import hash_password
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == "admin@grantthornton.in").first()
        if not existing:
            admin = User(
                name="Admin",
                email="admin@grantthornton.in",
                password_hash=hash_password("Admin@123"),
                role="admin",
            )
            db.add(admin)
            db.commit()
            print("[DB] Seeded default admin: admin@grantthornton.in / Admin@123")
        else:
            print("[DB] Tables ready.")
    finally:
        db.close()
