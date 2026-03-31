"""
Learning Store
--------------
Bridges user feedback → model improvement in two ways:

WAY 1 — Few-shot prompt injection
  When the LLM is about to extract a clause for a new RFP, this module
  retrieves past "correction examples" for the same (offering, solution,
  clause_type) and injects them into the prompt. The LLM sees:

      "Previous reviewers for ENERGY & RENEWABLES / RENEWABLES said:
       - The system extracted this clause as HIGH RISK. The correct
         assessment for this type of engagement is MEDIUM. Reason: ..."

  This is in-context learning — no fine-tuning needed. Works with any
  Ollama model. The effect compounds: more feedback → richer examples →
  better extraction and risk characterisation.

WAY 2 — Synthesised rule text
  An admin can trigger a synthesis run for any (offering, solution,
  clause_type) combination. This module collects all feedback + reviewer
  comments, then calls Ollama and asks it to write updated evaluation
  criteria. The result is stored as a LearnedRule and injected into the
  risk_engine prompts on future runs.

  Synthesis is idempotent — running it again on new feedback overwrites
  the previous rule. Admins can review and deactivate rules they disagree
  with via the /feedback/rules endpoints.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

OLLAMA_HOST  = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"

# Minimum feedback before we build a few-shot pool for a combo
MIN_EXAMPLES_FOR_INJECTION = 2

# Minimum feedback before synthesis is allowed
MIN_FEEDBACK_FOR_SYNTHESIS = 5


# ── Normalisation ─────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return (text or "").strip().upper()


def _matches_context(row_offering: str, row_solution: str,
                     target_offering: str, target_solution: str) -> bool:
    """Partial case-insensitive match — same logic as feedback_engine."""
    ro = _norm(row_offering)
    rs = _norm(row_solution)
    to = _norm(target_offering)
    ts = _norm(target_solution)
    off_match = (not to) or (to in ro) or (ro in to)
    sol_match = (not ts) or (ts in rs) or (rs in ts)
    return off_match and sol_match


# ══════════════════════════════════════════════════════════════════════════════
# WAY 1 — Few-shot example retrieval
# ══════════════════════════════════════════════════════════════════════════════

def build_fewshot_context(
    clause_type: str,
    offering: str,
    solution: str,
    db,
    max_examples: int = 4,
) -> str:
    """
    Returns a formatted string block to append to any LLM prompt.
    Empty string if there are not enough examples yet.

    The block looks like:

        === LEARNING CONTEXT FOR ENERGY & RENEWABLES / RENEWABLES ===
        Past reviewers have corrected assessments for this type of engagement:

        Example 1 (Liability — rated too HIGH by system):
          Clause snippet: "The consultant shall be liable for..."
          System said: HIGH RISK
          Reviewers said: MEDIUM RISK
          Why: "For renewables advisory work, liability rarely materialises
                at contract value. Cap at 50% is standard here."

        Example 2 ...
        === END LEARNING CONTEXT ===
    """
    from database import ClauseFeedback, LearningExample

    # Pull curated learning examples first (higher quality — admin-verified)
    curated = (
        db.query(LearningExample)
        .filter(
            LearningExample.clause_type == clause_type,
            LearningExample.is_active == True,
        )
        .order_by(LearningExample.usefulness_score.desc(), LearningExample.created_at.desc())
        .all()
    )
    curated = [e for e in curated if _matches_context(e.offering, e.solution, offering, solution)]

    # Also pull raw non-agree feedback as informal examples
    raw_feedback = (
        db.query(ClauseFeedback)
        .filter(
            ClauseFeedback.clause_type == clause_type,
            ClauseFeedback.agreement != "agree",
            ClauseFeedback.feedback_comment != None,
        )
        .order_by(ClauseFeedback.created_at.desc())
        .limit(30)
        .all()
    )
    raw_feedback = [
        f for f in raw_feedback
        if _matches_context(f.offering, f.solution, offering, solution)
           and f.feedback_comment and len(f.feedback_comment.strip()) > 10
    ]

    if len(curated) + len(raw_feedback) < MIN_EXAMPLES_FOR_INJECTION:
        return ""  # not enough signal yet

    lines = [
        f"\n\n=== LEARNING CONTEXT FOR {_norm(offering)} / {_norm(solution)} ===",
        f"Past reviewers have flagged the following patterns for {clause_type.upper()} clauses",
        f"in {offering or 'this type of'} / {solution or 'this type of'} engagements.",
        "Use these corrections to calibrate your assessment:\n",
    ]

    used = 0

    # Curated examples first
    for ex in curated[:max_examples]:
        used += 1
        lines.append(f"Correction {used}:")
        if ex.clause_snippet:
            snippet = ex.clause_snippet[:200] + ("..." if len(ex.clause_snippet) > 200 else "")
            lines.append(f"  Clause snippet: \"{snippet}\"")
        lines.append(f"  System assessed: {ex.system_risk_level}")
        lines.append(f"  Correct level:   {ex.correct_risk_level}")
        if ex.reviewer_reason:
            lines.append(f"  Reason: \"{ex.reviewer_reason}\"")
        lines.append("")

    # Supplement with raw feedback if needed
    for fb in raw_feedback[:max(0, max_examples - used)]:
        used += 1
        lines.append(f"Correction {used}:")
        lines.append(f"  System assessed: {fb.system_risk_level}")
        if fb.suggested_risk_level:
            lines.append(f"  Reviewer suggested: {fb.suggested_risk_level}")
        lines.append(f"  Reviewer comment: \"{fb.feedback_comment.strip()}\"")
        lines.append("")

    lines.append("=== END LEARNING CONTEXT ===\n")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# WAY 2 — Rule synthesis via Ollama
# ══════════════════════════════════════════════════════════════════════════════

SYNTHESIS_PROMPT_TEMPLATE = """
You are a senior legal risk consultant at Grant Thornton (GTBL).

You are updating the risk evaluation criteria for the following context:
  Clause type:  {clause_type}
  Offering:     {offering}
  Solution:     {solution}

Below is a summary of how past reviewers corrected the system's assessments
for this specific type of engagement. Study the patterns carefully.

FEEDBACK SUMMARY:
{feedback_summary}

CURRENT STATIC RULE (what the system uses today):
{current_static_rule}

Your task:
Write UPDATED evaluation criteria for this clause type that would have
produced the correct assessments in the above cases.

Return ONLY valid JSON — no explanation, no preamble:
{{
  "updated_rule_text": "clear, specific rule text in 3-6 sentences explaining what to look for, what constitutes HIGH vs MEDIUM vs ACCEPTABLE for THIS type of engagement specifically",
  "risk_threshold_notes": {{
    "HIGH": "what specifically makes this clause HIGH risk in {offering}/{solution} context",
    "MEDIUM": "what specifically makes this clause MEDIUM risk",
    "ACCEPTABLE": "what conditions make this clause acceptable"
  }},
  "key_differences_from_default": "1-3 sentences on how this context differs from the general rule",
  "confidence": "HIGH | MEDIUM | LOW based on how consistent the reviewer feedback was"
}}
"""

# Current static rules summary (injected into synthesis prompt for context)
STATIC_RULE_SUMMARIES = {
    "liability": "Uncapped or cap > contract value → HIGH. Cap = contract value → MEDIUM. Cap < contract value → ACCEPTABLE.",
    "insurance": "Client named as co-insured or insurance requires client approval → HIGH. Otherwise LOW.",
    "scope": "High-risk activities (civil works, DPR, supervision, legal services, AI decisions) → HIGH. Otherwise LOW.",
    "payment": "No invoice cycle specified → MEDIUM. No approval timeline → MEDIUM. Both present → ACCEPTABLE.",
    "deliverables": "Overlapping deliverables, unclear acceptance, aggressive timelines, missing client deps → MEDIUM. Otherwise LOW.",
    "personnel": "Replacement period ≤30 days or absent → MEDIUM. >30 days → ACCEPTABLE.",
    "ld": "Uncapped or ≥20% → HIGH. 10-20% → MEDIUM. ≤10% → ACCEPTABLE.",
    "penalties": "Same thresholds as LDs.",
    "termination": "Only client can terminate (unilateral) → HIGH. GTBL has symmetric rights → ACCEPTABLE.",
    "eligibility": "Any blacklisting/termination/penalty declaration conflicting with GTBL history → HIGH.",
}


def _build_feedback_summary(feedbacks) -> str:
    """Format a list of ClauseFeedback rows into a readable synthesis input."""
    if not feedbacks:
        return "No feedback available."

    lines = []
    for i, fb in enumerate(feedbacks, 1):
        lines.append(f"Review {i}:")
        lines.append(f"  System said:    {fb.system_risk_level}")
        lines.append(f"  Reviewer verdict: {fb.agreement}")
        if fb.suggested_risk_level:
            lines.append(f"  Reviewer suggested: {fb.suggested_risk_level}")
        if fb.feedback_comment:
            lines.append(f"  Reviewer comment: \"{fb.feedback_comment.strip()}\"")
        lines.append("")

    return "\n".join(lines)


def synthesise_rule(
    clause_type: str,
    offering: str,
    solution: str,
    db,
    model: str = OLLAMA_MODEL,
    force: bool = False,
) -> dict:
    """
    Synthesise an updated rule for (offering, solution, clause_type) from
    accumulated feedback. Stores result as a LearnedRule in the DB.

    Returns:
    {
        "status": "created" | "updated" | "skipped",
        "reason": str,
        "rule": dict | None,
    }
    """
    from database import ClauseFeedback, LearnedRule

    # Collect relevant feedback
    all_fb = (
        db.query(ClauseFeedback)
        .filter(ClauseFeedback.clause_type == clause_type)
        .order_by(ClauseFeedback.created_at.desc())
        .all()
    )
    relevant = [
        fb for fb in all_fb
        if _matches_context(fb.offering, fb.solution, offering, solution)
    ]

    if len(relevant) < MIN_FEEDBACK_FOR_SYNTHESIS and not force:
        return {
            "status": "skipped",
            "reason": f"Only {len(relevant)} feedback entries (need {MIN_FEEDBACK_FOR_SYNTHESIS}).",
            "rule": None,
        }

    feedback_summary = _build_feedback_summary(relevant)
    current_static = STATIC_RULE_SUMMARIES.get(clause_type, "No static rule defined.")

    prompt = SYNTHESIS_PROMPT_TEMPLATE.format(
        clause_type=clause_type,
        offering=offering or "general",
        solution=solution or "general",
        feedback_summary=feedback_summary,
        current_static_rule=current_static,
    )

    # Call Ollama
    try:
        import ollama
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1},
        )
        raw = response["message"]["content"]

        # Clean and parse JSON
        raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        parsed = json.loads(raw[start:end])

    except Exception as e:
        return {
            "status": "error",
            "reason": f"LLM synthesis failed: {e}",
            "rule": None,
        }

    rule_text           = parsed.get("updated_rule_text", "")
    threshold_notes     = parsed.get("risk_threshold_notes", {})
    key_differences     = parsed.get("key_differences_from_default", "")
    confidence          = parsed.get("confidence", "LOW")

    # Upsert LearnedRule
    existing = (
        db.query(LearnedRule)
        .filter(
            LearnedRule.clause_type == clause_type,
            LearnedRule.offering    == _norm(offering),
            LearnedRule.solution    == _norm(solution),
        )
        .first()
    )

    if existing:
        existing.rule_text              = rule_text
        existing.threshold_notes_json   = json.dumps(threshold_notes)
        existing.key_differences        = key_differences
        existing.confidence             = confidence
        existing.feedback_count_at_gen  = len(relevant)
        existing.generated_at           = datetime.utcnow()
        existing.is_active              = True
        db.commit()
        db.refresh(existing)
        action = "updated"
        rule_row = existing
    else:
        rule_row = LearnedRule(
            clause_type             = clause_type,
            offering                = _norm(offering),
            solution                = _norm(solution),
            rule_text               = rule_text,
            threshold_notes_json    = json.dumps(threshold_notes),
            key_differences         = key_differences,
            confidence              = confidence,
            feedback_count_at_gen   = len(relevant),
            is_active               = True,
        )
        db.add(rule_row)
        db.commit()
        db.refresh(rule_row)
        action = "created"

    print(f"[LearningStore] Rule {action} for {clause_type}/{offering}/{solution} "
          f"(confidence={confidence}, from {len(relevant)} feedbacks)")

    return {
        "status": action,
        "reason": f"Synthesised from {len(relevant)} feedback entries.",
        "rule": learned_rule_to_dict(rule_row),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Retrieve learned rule for injection into risk engine
# ══════════════════════════════════════════════════════════════════════════════

def get_learned_rule(clause_type: str, offering: str, solution: str, db) -> Optional[str]:
    """
    Returns the synthesised rule text for (offering, solution, clause_type)
    if one exists and is active. Returns None otherwise.

    This is called by the risk engine before applying static rules.
    """
    from database import LearnedRule

    rows = (
        db.query(LearnedRule)
        .filter(
            LearnedRule.clause_type == clause_type,
            LearnedRule.is_active   == True,
        )
        .all()
    )

    for row in rows:
        if _matches_context(row.offering, row.solution, offering, solution):
            return row.rule_text

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Create learning example from feedback (for curated pool)
# ══════════════════════════════════════════════════════════════════════════════

def create_learning_example(feedback_id: int, db, clause_snippet: str = "") -> bool:
    """
    Promotes a feedback record into a curated LearningExample if it has
    a suggested_risk_level and feedback_comment (i.e. it's high-quality).
    Called automatically when feedback is submitted.
    """
    from database import ClauseFeedback, LearningExample

    fb = db.query(ClauseFeedback).filter(ClauseFeedback.id == feedback_id).first()
    if not fb:
        return False

    # Only create examples for non-agree feedback with a comment
    if fb.agreement == "agree" or not fb.feedback_comment:
        return False

    # Check for duplicate
    existing = (
        db.query(LearningExample)
        .filter(LearningExample.feedback_id == feedback_id)
        .first()
    )
    if existing:
        return False

    # Get clause text snippet from the clause result if available
    if not clause_snippet and fb.clause_result_id:
        from database import ClauseResult
        cr = db.query(ClauseResult).filter(ClauseResult.id == fb.clause_result_id).first()
        if cr and cr.clause_text:
            clause_snippet = cr.clause_text[:300]

    example = LearningExample(
        feedback_id        = fb.id,
        clause_type        = fb.clause_type,
        offering           = fb.offering or "",
        solution           = fb.solution or "",
        bu                 = fb.bu or "",
        clause_snippet     = clause_snippet,
        system_risk_level  = fb.system_risk_level,
        correct_risk_level = fb.suggested_risk_level or "",
        reviewer_reason    = fb.feedback_comment,
        usefulness_score   = 1,   # default; admin can adjust
        is_active          = True,
    )
    db.add(example)
    db.commit()
    return True


# ── Serialisers ───────────────────────────────────────────────────────────────

def learned_rule_to_dict(rule) -> dict:
    threshold_notes = {}
    try:
        threshold_notes = json.loads(rule.threshold_notes_json or "{}")
    except Exception:
        pass
    return {
        "id":                    rule.id,
        "clause_type":           rule.clause_type,
        "offering":              rule.offering,
        "solution":              rule.solution,
        "rule_text":             rule.rule_text,
        "threshold_notes":       threshold_notes,
        "key_differences":       rule.key_differences,
        "confidence":            rule.confidence,
        "feedback_count_at_gen": rule.feedback_count_at_gen,
        "is_active":             rule.is_active,
        "generated_at":          rule.generated_at.isoformat() if rule.generated_at else None,
    }


def learning_example_to_dict(ex) -> dict:
    return {
        "id":                ex.id,
        "feedback_id":       ex.feedback_id,
        "clause_type":       ex.clause_type,
        "offering":          ex.offering,
        "solution":          ex.solution,
        "bu":                ex.bu,
        "clause_snippet":    ex.clause_snippet,
        "system_risk_level": ex.system_risk_level,
        "correct_risk_level":ex.correct_risk_level,
        "reviewer_reason":   ex.reviewer_reason,
        "usefulness_score":  ex.usefulness_score,
        "is_active":         ex.is_active,
        "created_at":        ex.created_at.isoformat() if ex.created_at else None,
    }