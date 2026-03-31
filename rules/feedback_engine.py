"""
Feedback Engine
---------------
Aggregates user feedback (per offering × solution × clause_type) to derive
risk-level adjustments that improve the system's assessments over time.

How it works
────────────
1. After a user reviews a clause they submit structured feedback:
     - agreement:            agree | too_high | too_low | incorrect
     - suggested_risk_level: HIGH | MEDIUM | ACCEPTABLE | LOW | NEEDS_REVIEW
     - comment:              free text

2. This module queries historical feedback for the same
   (offering, solution, clause_type) combination and computes:
     - consensus_direction:  LOWER | HIGHER | NONE
     - confidence:           float 0–1 (fraction agreeing on direction)
     - suggested_override:   the most-voted suggested risk level (if any)

3. The pipeline calls `get_adjustment()` before persisting a ClauseResult.
   If a strong consensus exists, the stored risk_level is annotated with
   `_ADJUSTED` and the original system level is preserved separately.
   The API response includes both so the frontend can display
   "System: HIGH → Adjusted to: MEDIUM (based on 8 past reviews)"

Minimum thresholds (tune in constants below):
    MIN_FEEDBACK = 3      ← ignore combos with fewer than 3 data points
    CONSENSUS_THRESHOLD = 0.60  ← 60 % agreement on direction required
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_FEEDBACK          = 1    # minimum feedback points before we trust the aggregate
CONSENSUS_THRESHOLD   = 0.50  # fraction of feedback in one direction required

RISK_ORDER = ["ACCEPTABLE", "LOW", "MEDIUM", "HIGH"]   # ascending severity

# ── Risk-level arithmetic ──────────────────────────────────────────────────────

def _bump_up(level: str) -> str:
    """Return next higher risk level."""
    idx = RISK_ORDER.index(level) if level in RISK_ORDER else 1
    return RISK_ORDER[min(idx + 1, len(RISK_ORDER) - 1)]


def _bump_down(level: str) -> str:
    """Return next lower risk level."""
    idx = RISK_ORDER.index(level) if level in RISK_ORDER else 2
    return RISK_ORDER[max(idx - 1, 0)]


# ── Normalise offering / solution strings for comparison ──────────────────────

def _norm(text: str) -> str:
    return (text or "").strip().upper()


# ── Main aggregation function ──────────────────────────────────────────────────

def get_adjustment(
    clause_type: str,
    offering: str,
    solution: str,
    system_risk_level: str,
    db,                    # SQLAlchemy Session — passed in to avoid circular import
) -> dict:
    """
    Returns an adjustment dict for a given (offering, solution, clause_type).

    Return shape:
    {
        "adjusted_risk_level": str,          # possibly same as system_risk_level
        "system_risk_level": str,            # original system assessment
        "direction": "LOWER"|"HIGHER"|"NONE",
        "confidence": float,                 # 0–1
        "feedback_count": int,
        "top_suggested_level": str|None,     # most-voted user suggestion
        "applied": bool,                     # True if we actually changed the level
        "reason": str,                       # human-readable explanation
    }
    """
    from database import ClauseFeedback   # local import avoids circular dep

    norm_offering = _norm(offering)
    norm_solution = _norm(solution)

    # ── Query: exact offering+solution match first, then offering-only fallback ─
    rows = (
        db.query(ClauseFeedback)
        .filter(ClauseFeedback.clause_type == clause_type)
        .all()
    )

    # Filter in Python (case-insensitive partial match on offering & solution)
    def _matches(row):
        row_off = _norm(row.offering or "")
        row_sol = _norm(row.solution or "")
        off_match = (not norm_offering) or (norm_offering in row_off) or (row_off in norm_offering)
        sol_match = (not norm_solution) or (norm_solution in row_sol) or (row_sol in norm_solution)
        return off_match and sol_match

    matched = [r for r in rows if _matches(r)]

    no_adjustment = {
        "adjusted_risk_level": system_risk_level,
        "system_risk_level":   system_risk_level,
        "direction":           "NONE",
        "confidence":          0.0,
        "feedback_count":      len(matched),
        "top_suggested_level": None,
        "applied":             False,
        "reason":              f"Insufficient feedback ({len(matched)} < {MIN_FEEDBACK} required).",
    }

    if len(matched) < MIN_FEEDBACK:
        return no_adjustment

    # ── Count directions ───────────────────────────────────────────────────────
    direction_counts = Counter(r.agreement for r in matched if r.agreement)
    total = len(matched)

    too_high_pct = direction_counts.get("too_high", 0) / total
    too_low_pct  = direction_counts.get("too_low",  0) / total

    # ── Most-voted suggested risk level ───────────────────────────────────────
    suggested_levels = [r.suggested_risk_level for r in matched if r.suggested_risk_level]
    top_suggested = Counter(suggested_levels).most_common(1)[0][0] if suggested_levels else None

    # ── Apply direction ────────────────────────────────────────────────────────
    if too_high_pct >= CONSENSUS_THRESHOLD:
        # Users consistently say system is rating too high → lower it
        adjusted = _bump_down(system_risk_level)
        # If there's a clear voted level, prefer that
        if top_suggested and top_suggested in RISK_ORDER:
            adjusted = top_suggested
        return {
            "adjusted_risk_level": adjusted,
            "system_risk_level":   system_risk_level,
            "direction":           "LOWER",
            "confidence":          round(too_high_pct, 2),
            "feedback_count":      total,
            "top_suggested_level": top_suggested,
            "applied":             adjusted != system_risk_level,
            "reason": (
                f"{int(too_high_pct*100)}% of {total} reviewers for "
                f"'{offering} / {solution}' said this clause was rated too high."
            ),
        }

    if too_low_pct >= CONSENSUS_THRESHOLD:
        # Users consistently say system is rating too low → raise it
        adjusted = _bump_up(system_risk_level)
        if top_suggested and top_suggested in RISK_ORDER:
            adjusted = top_suggested
        return {
            "adjusted_risk_level": adjusted,
            "system_risk_level":   system_risk_level,
            "direction":           "HIGHER",
            "confidence":          round(too_low_pct, 2),
            "feedback_count":      total,
            "top_suggested_level": top_suggested,
            "applied":             adjusted != system_risk_level,
            "reason": (
                f"{int(too_low_pct*100)}% of {total} reviewers for "
                f"'{offering} / {solution}' said this clause was rated too low."
            ),
        }

    return {
        "adjusted_risk_level": system_risk_level,
        "system_risk_level":   system_risk_level,
        "direction":           "NONE",
        "confidence":          max(too_high_pct, too_low_pct),
        "feedback_count":      total,
        "top_suggested_level": top_suggested,
        "applied":             False,
        "reason":              f"No strong consensus across {total} reviews (threshold: {int(CONSENSUS_THRESHOLD*100)}%).",
    }


# ── Summary for admin insights panel ──────────────────────────────────────────

def get_feedback_insights(db) -> list[dict]:
    """
    Aggregates ALL feedback into per-(offering, solution, clause_type) summaries.
    Used by the admin insights endpoint.
    """
    from database import ClauseFeedback

    rows = db.query(ClauseFeedback).all()

    # Group rows by (offering, solution, clause_type)
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (_norm(r.offering), _norm(r.solution), r.clause_type)
        groups.setdefault(key, []).append(r)

    insights = []
    for (offering, solution, clause_type), group in sorted(groups.items()):
        direction_counts = Counter(g.agreement for g in group)
        suggested_levels = [g.suggested_risk_level for g in group if g.suggested_risk_level]
        top_suggested = Counter(suggested_levels).most_common(1)[0][0] if suggested_levels else None
        system_levels = [g.system_risk_level for g in group if g.system_risk_level]
        typical_system = Counter(system_levels).most_common(1)[0][0] if system_levels else "UNKNOWN"

        total = len(group)
        agree_pct     = round(direction_counts.get("agree",     0) / total, 2)
        too_high_pct  = round(direction_counts.get("too_high",  0) / total, 2)
        too_low_pct   = round(direction_counts.get("too_low",   0) / total, 2)
        incorrect_pct = round(direction_counts.get("incorrect", 0) / total, 2)

        insights.append({
            "offering":          offering,
            "solution":          solution,
            "clause_type":       clause_type,
            "feedback_count":    total,
            "typical_system_level": typical_system,
            "top_suggested_level":  top_suggested,
            "agreement_breakdown": {
                "agree":     agree_pct,
                "too_high":  too_high_pct,
                "too_low":   too_low_pct,
                "incorrect": incorrect_pct,
            },
            "recommendation": (
                "Lower risk rating"  if too_high_pct  >= CONSENSUS_THRESHOLD else
                "Raise risk rating"  if too_low_pct   >= CONSENSUS_THRESHOLD else
                "No change needed"   if agree_pct     >= CONSENSUS_THRESHOLD else
                "Mixed opinions"
            ),
            "has_strong_signal": (
                too_high_pct >= CONSENSUS_THRESHOLD or
                too_low_pct  >= CONSENSUS_THRESHOLD or
                agree_pct    >= CONSENSUS_THRESHOLD
            ),
        })

    # Sort: strong signals first, then by feedback count desc
    insights.sort(key=lambda x: (not x["has_strong_signal"], -x["feedback_count"]))
    return insights