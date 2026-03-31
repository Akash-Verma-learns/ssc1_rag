"""
Database
--------
SQLAlchemy models + Postgres connection.
Tables are auto-created on first run.

Tables:
  users             — auth
  rfps              — uploaded tenders
  clause_results    — extracted + risk-evaluated clause data
  comments          — per-clause reviewer comments
  clause_feedback   — structured reviewer feedback
  learning_examples — curated few-shot examples for prompt injection
  learned_rules     — LLM-synthesised evaluation rules per offering/solution/clause
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# Tables
# ══════════════════════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(100), nullable=False)
    email         = Column(String(150), unique=True, index=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    role          = Column(Enum("admin", "reviewer", name="user_role"), default="reviewer")
    created_at    = Column(DateTime, default=datetime.utcnow)

    rfps      = relationship("RFP", back_populates="uploaded_by_user")
    comments  = relationship("Comment", back_populates="user")
    feedbacks = relationship("ClauseFeedback", back_populates="user")


class RFP(Base):
    __tablename__ = "rfps"

    id               = Column(Integer, primary_key=True, index=True)
    opportunity_name = Column(String(300), nullable=False)
    client_name      = Column(String(150))
    bu               = Column(String(150))
    classification   = Column(String(50))
    state            = Column(String(100))
    country          = Column(String(100))
    offering         = Column(String(500))
    solutions        = Column(String(500))
    file_name        = Column(String(300))
    job_id           = Column(String(50), unique=True, index=True)
    status           = Column(String(30), default="queued")
    progress         = Column(Integer, default=0)
    current_step     = Column(String(100), default="")
    error_message    = Column(Text, nullable=True)
    uploaded_by      = Column(Integer, ForeignKey("users.id"))
    created_at       = Column(DateTime, default=datetime.utcnow)

    uploaded_by_user = relationship("User", back_populates="rfps")
    clause_results   = relationship("ClauseResult", back_populates="rfp", cascade="all, delete")
    comments         = relationship("Comment", back_populates="rfp", cascade="all, delete")
    feedbacks        = relationship("ClauseFeedback", back_populates="rfp", cascade="all, delete")


class ClauseResult(Base):
    __tablename__ = "clause_results"

    id                    = Column(Integer, primary_key=True, index=True)
    rfp_id                = Column(Integer, ForeignKey("rfps.id"), nullable=False)
    clause_type           = Column(String(50))
    clause_text           = Column(Text)
    clause_reference      = Column(String(200))
    page_no               = Column(String(20))
    risk_level            = Column(String(30))       # raw system assessment
    risk_description      = Column(Text)
    auto_remark           = Column(Text)
    needs_exception       = Column(Boolean, default=False)
    needs_eqcr            = Column(Boolean, default=False)
    deviation_suggested   = Column(Text)

    # Feedback engine adjustments
    adjusted_risk_level   = Column(String(30), nullable=True)
    adjustment_reason     = Column(Text, nullable=True)
    adjustment_confidence = Column(String(10), nullable=True)
    feedback_count        = Column(Integer, default=0)

    # Whether a learned rule influenced this result
    learned_rule_applied  = Column(Boolean, default=False)
    learned_rule_id       = Column(Integer, ForeignKey("learned_rules.id"), nullable=True)

    rfp          = relationship("RFP", back_populates="clause_results")
    feedbacks    = relationship("ClauseFeedback", back_populates="clause_result",
                                foreign_keys="ClauseFeedback.clause_result_id")
    learned_rule = relationship("LearnedRule", foreign_keys=[learned_rule_id])


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


class ClauseFeedback(Base):
    """
    Structured reviewer feedback on a single clause's risk assessment.

    agreement values:
      agree      — system is correct for this offering/solution
      too_high   — system over-rated the risk for this type of work
      too_low    — system under-rated the risk for this type of work
      incorrect  — logic is wrong (free text comment explains why)

    offering + solution are snapshotted at submission time so historical
    feedback stays meaningful even if the RFP record is later edited.
    """
    __tablename__ = "clause_feedback"

    id                   = Column(Integer, primary_key=True, index=True)
    rfp_id               = Column(Integer, ForeignKey("rfps.id"), nullable=False)
    clause_result_id     = Column(Integer, ForeignKey("clause_results.id"), nullable=True)
    clause_type          = Column(String(50), nullable=False, index=True)
    user_id              = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Context snapshot
    offering             = Column(String(500))
    solution             = Column(String(500))
    bu                   = Column(String(150))

    # Feedback
    agreement            = Column(
        Enum("agree", "too_high", "too_low", "incorrect", name="feedback_agreement"),
        nullable=False,
    )
    suggested_risk_level = Column(String(30), nullable=True)
    feedback_comment     = Column(Text, nullable=True)
    system_risk_level    = Column(String(30), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    rfp              = relationship("RFP", back_populates="feedbacks")
    user             = relationship("User", back_populates="feedbacks")
    clause_result    = relationship("ClauseResult", back_populates="feedbacks",
                                    foreign_keys=[clause_result_id])
    learning_example = relationship("LearningExample", back_populates="feedback",
                                    uselist=False)


class LearningExample(Base):
    """
    Curated few-shot examples derived from reviewer feedback.

    Injected into LLM extraction and risk-evaluation prompts when analysing
    future RFPs that share the same offering/solution/clause_type context.
    Created automatically on feedback submission (when comment is present).
    Admins can deactivate low-quality examples or boost high-quality ones
    via usefulness_score.
    """
    __tablename__ = "learning_examples"

    id                 = Column(Integer, primary_key=True, index=True)
    feedback_id        = Column(Integer, ForeignKey("clause_feedback.id"), nullable=True)
    clause_type        = Column(String(50), nullable=False, index=True)
    offering           = Column(String(500), index=True)
    solution           = Column(String(500))
    bu                 = Column(String(150))

    clause_snippet     = Column(Text)           # first 300 chars of extracted clause text
    system_risk_level  = Column(String(30))     # what the model said
    correct_risk_level = Column(String(30))     # what the reviewer said it should be
    reviewer_reason    = Column(Text)           # the reviewer's comment

    usefulness_score   = Column(Integer, default=1)   # 1=normal, 2=high, 3=essential
    is_active          = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)

    feedback = relationship("ClauseFeedback", back_populates="learning_example")


class LearnedRule(Base):
    """
    LLM-synthesised evaluation rules, generated from accumulated feedback
    via POST /feedback/synthesise.

    The rule_text overrides the static rule in risk_engine.py for the
    specific (offering, solution, clause_type) combination it was generated
    for. Admins can deactivate rules without deleting them.
    """
    __tablename__ = "learned_rules"

    id                    = Column(Integer, primary_key=True, index=True)
    clause_type           = Column(String(50), nullable=False, index=True)
    offering              = Column(String(500), index=True)
    solution              = Column(String(500))

    rule_text             = Column(Text, nullable=False)
    threshold_notes_json  = Column(Text)         # JSON: {HIGH: ..., MEDIUM: ..., ACCEPTABLE: ...}
    key_differences       = Column(Text)         # how this differs from the static rule
    confidence            = Column(String(10))   # HIGH / MEDIUM / LOW

    feedback_count_at_gen = Column(Integer, default=0)
    is_active             = Column(Boolean, default=True)
    generated_at          = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# Init
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    Base.metadata.create_all(bind=engine)
    _safe_alter_columns()

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


def _safe_alter_columns():
    from sqlalchemy import text
    alterations = [
        "ALTER TABLE rfps ADD COLUMN IF NOT EXISTS country VARCHAR(100)",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS adjusted_risk_level VARCHAR(30)",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS adjustment_reason TEXT",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS adjustment_confidence VARCHAR(10)",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS feedback_count INTEGER DEFAULT 0",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS learned_rule_applied BOOLEAN DEFAULT FALSE",
        "ALTER TABLE clause_results ADD COLUMN IF NOT EXISTS learned_rule_id INTEGER",
    ]
    try:
        with engine.connect() as conn:
            for stmt in alterations:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
    except Exception:
        pass