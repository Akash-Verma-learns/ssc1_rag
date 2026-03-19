"""
Risk Rules Engine
-----------------
Pure Python. No LLM. Applies GTBL's deterministic thresholds to extracted clause data.

This is intentionally NOT handled by the LLM — rules must be consistent and auditable.
The LLM extracts facts; this module evaluates them.

Risk levels: "HIGH" | "MEDIUM" | "LOW" | "ACCEPTABLE" | "NEEDS_REVIEW"
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskResult:
    clause_name: str
    risk_level: str                      # HIGH / MEDIUM / LOW / ACCEPTABLE / NEEDS_REVIEW
    risk_description: str                # What specifically triggered the risk
    auto_remark: str                     # Pre-written R&Q remark text
    needs_exception_approval: bool = False
    needs_eqcr: bool = False
    deviation_suggested: str = ""        # Suggested modified language (for eligibility)


# ──────────────────────────────────────────────────────────────────────────────
# Pre-written remark templates (from Clause_wise_instructions.docx)
# ──────────────────────────────────────────────────────────────────────────────

REMARKS = {
    "liability_high": (
        "It is suggested to request the Client that the overall liabilities are capped at the contract value. "
        "Additionally, EQCR shall be applicable and ET shall account for an EQCR at this stage. "
        "It is also suggested to propose GTBL's standard assumption relating to limitation of liability as part of the proposal. "
        "Exception approval to be sought from the concerned BUL prior to bid submission."
    ),
    "insurance_high": (
        "ET to note OGC comments and seek clarity from the Client. "
        "Once the Client responds, ET to reach out to the Risk & Quality Pillar team for determining the way forward."
    ),
    "payment_no_invoice_cycle": (
        "It is suggested to propose that the invoice to payment cycle is kept as 30 days."
    ),
    "payment_no_approval_timeline": (
        "It is suggested to propose that if the Client fails to provide any comments/suggestions "
        "within 15 days of submission of deliverables, the same shall be deemed to have been duly approved."
    ),
    "personnel_replacement_short": (
        "It is suggested to request the Client for a replacement period of at least 30 days, "
        "in cases where replacement is necessitated for reasons beyond the control of the "
        "Selected Bidder/Agency/Consultant."
    ),
    "ld_high": (
        "It is suggested to request the Client that the overall LDs are capped at 10% of the contract value. "
        "Exception approval to be sought from the BUL prior to bid submission. "
        "EQCR shall be applicable and ET to account for it."
    ),
    "ld_medium": (
        "It is suggested to request the Client that the overall LDs are capped at 10% of the contract value."
    ),
    "penalties_high": (
        "It is suggested to request the Client that the overall penalties are capped at 10% of the contract value. "
        "Exception approval to be sought from the BUL prior to bid submission. "
        "EQCR shall be applicable and ET to account for it."
    ),
    "penalties_medium": (
        "It is suggested to request the Client that the overall penalties are capped at 10% of the contract value."
    ),
    "termination_unilateral": (
        "It is suggested to request the Client that the Consultant/Agency/Selected Bidder has termination rights "
        "in cases of non-payment by the Client within the prescribed time period or if the Client fails to cure "
        "a material breach within 21 days of notice of such breach. "
        "Exception approval to be sought before bid submission from the concerned BUL."
    ),
    "eligibility_blacklisting": (
        "GTBL was blacklisted/debarred from October 2021 to September 2024. The eligibility declaration as "
        "written would result in misrepresentation. It is suggested to seek a deviation so that the declaration "
        "is made 'as on the date of submission'. Where the clause uses 'has not been' or 'have not been', "
        "it must be modified to 'is not' as on date. Minimum modification language to be proposed. "
        "Exception approval to be sought from the concerned BUL prior to bid submission."
    ),
    "eligibility_termination_penalty": (
        "GTBL currently faces a penalty for non-performance and has an amicable closure (w.e.f. 09.01.2026) "
        "for a prior termination. Any declaration requiring a clean record for terminations or penalties "
        "would require a deviation. It is suggested to propose that the declaration be made 'as on the date "
        "of submission' or modified to 'is not currently subject to' language. "
        "Exception approval to be sought from the concerned BUL prior to bid submission."
    ),
    "no_deviation_bid": (
        "This RFP contains a no-deviation clause / unconditional acceptance clause. "
        "Any modification to eligibility declarations may lead to bid disqualification. "
        "Legal guidance and BUL exception approval must be sought before bid submission."
    ),
}

# GTBL factual position (injected into LLM context for eligibility checks)
GTBL_FACTUAL_POSITION = {
    "blacklisted_from": "October 2021",
    "blacklisted_to": "September 2024",
    "current_penalty": True,          # penalty for non-performance exists
    "prior_termination": True,        # terminated by client, now amicable closure w.e.f. 09.01.2026
    "amicable_closure_date": "09.01.2026",
}


# ──────────────────────────────────────────────────────────────────────────────
# Individual clause evaluators
# ──────────────────────────────────────────────────────────────────────────────

def _parse_percentage(text: str) -> Optional[float]:
    """Extract the first percentage figure from text."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def _is_uncapped(text: str) -> bool:
    """Check if text suggests no cap / unlimited liability."""
    uncapped_phrases = [
        "unlimited", "uncapped", "no cap", "no limit", "without limit",
        "without any limit", "without any cap", "not limited", "not capped",
        "full liability", "entire liability",
    ]
    t = text.lower()
    return any(p in t for p in uncapped_phrases)


def evaluate_liability(extracted: dict) -> RiskResult:
    """
    Rules:
      - Uncapped / cap > contract value → HIGH RISK
      - Cap = contract value → MEDIUM (acceptable but flag)
      - Cap < contract value → ACCEPTABLE
    """
    clause_text = extracted.get("clause_text", "").lower()
    cap_info = extracted.get("cap_info", "")  # LLM-extracted: "uncapped" / "2x contract" / "contract value" / "50%"

    if _is_uncapped(clause_text) or _is_uncapped(cap_info):
        return RiskResult(
            clause_name="Limitation of Liability",
            risk_level="HIGH",
            risk_description="Liability is uncapped or not limited. GTBL faces unlimited exposure.",
            auto_remark=REMARKS["liability_high"],
            needs_exception_approval=True,
            needs_eqcr=True,
        )

    cap_lower = cap_info.lower()
    high_cap_phrases = ["greater than", "more than", "exceed", "2x", "twice", "double",
                        "higher than contract", "above contract"]
    if any(p in cap_lower for p in high_cap_phrases):
        return RiskResult(
            clause_name="Limitation of Liability",
            risk_level="HIGH",
            risk_description=f"Liability cap exceeds contract value: '{cap_info}'",
            auto_remark=REMARKS["liability_high"],
            needs_exception_approval=True,
            needs_eqcr=True,
        )

    if cap_info:
        return RiskResult(
            clause_name="Limitation of Liability",
            risk_level="ACCEPTABLE",
            risk_description=f"Liability appears capped at or below contract value: '{cap_info}'",
            auto_remark="",
        )

    # No clear info found — flag for review
    return RiskResult(
        clause_name="Limitation of Liability",
        risk_level="NEEDS_REVIEW",
        risk_description="Could not determine liability cap from the clause text. Manual review required.",
        auto_remark=REMARKS["liability_high"],
    )


def evaluate_insurance(extracted: dict) -> RiskResult:
    """
    Rules:
      - Client named as co-insured → HIGH
      - Insurance policies require client approval → HIGH
    """
    clause_text = extracted.get("clause_text", "").lower()
    flags = extracted.get("flags", [])

    high_risk_phrases = [
        "co-insured", "co insured", "named as insured",
        "approval of insurance", "insurance policies for approval",
        "submit insurance for approval", "client approval",
    ]

    triggered = [p for p in high_risk_phrases if p in clause_text]
    triggered += [f for f in flags if "co-insured" in f.lower() or "approval" in f.lower()]

    if triggered:
        return RiskResult(
            clause_name="Insurance Clause",
            risk_level="HIGH",
            risk_description=f"Triggered risk conditions: {', '.join(set(triggered))}",
            auto_remark=REMARKS["insurance_high"],
        )

    return RiskResult(
        clause_name="Insurance Clause",
        risk_level="LOW",
        risk_description="No high-risk insurance conditions identified.",
        auto_remark="",
    )


def evaluate_scope(extracted: dict) -> RiskResult:
    """
    Rules: flag if scope contains any of the high-risk activities.
    """
    clause_text = extracted.get("clause_text", "").lower()
    summary = extracted.get("summary", "").lower()
    combined = clause_text + " " + summary

    HIGH_RISK_SCOPE = {
        "civil engineering works": "Civil engineering works",
        "dpr preparation": "DPR preparation",
        "detailed project report": "DPR preparation",
        "supervision of construction": "Supervision/certification of construction",
        "certification of construction": "Supervision/certification of construction",
        "verify third party": "Verification of third-party works",
        "verification of third-party": "Verification of third-party works",
        "approve grants": "Recommendation/approval of grants or payments",
        "approve payments": "Recommendation/approval of grants or payments",
        "recommend payments": "Recommendation/approval of grants or payments",
        "safety of lives": "Safety of lives/personnel",
        "safety of personnel": "Safety of lives/personnel",
        "gambling": "Gambling or restricted activities",
        "legal services": "Legal services (GTBL not authorised)",
        "legal advisory": "Legal services (GTBL not authorised)",
        "representation before court": "Legal services (GTBL not authorised)",
        "ai decision": "AI-related decision making",
        "ai-related decision": "AI-related decision making",
        "audit firm": "RFP meant for audit firms (not consulting)",
        "architectural firm": "RFP meant for architectural firms (not consulting)",
    }

    triggered = {}
    for keyword, label in HIGH_RISK_SCOPE.items():
        if keyword in combined:
            triggered[label] = True

    if triggered:
        risk_items = list(triggered.keys())
        return RiskResult(
            clause_name="Scope of Work",
            risk_level="HIGH",
            risk_description=f"High-risk scope activities detected: {'; '.join(risk_items)}",
            auto_remark=(
                f"The scope of work includes the following high-risk activities which increase GTBL's risk exposure: "
                f"{'; '.join(risk_items)}. "
                "EQCR shall be applicable. Exception approval to be sought from the BUL prior to bid submission."
            ),
            needs_eqcr=True,
        )

    return RiskResult(
        clause_name="Scope of Work",
        risk_level="LOW",
        risk_description="No high-risk scope activities identified.",
        auto_remark="",
    )


def evaluate_payment_terms(extracted: dict) -> RiskResult:
    """
    Rules:
      - No invoice-to-payment cycle specified → add remark
      - No deliverable approval timeline → add remark
    """
    clause_text = extracted.get("clause_text", "").lower()
    has_invoice_cycle = extracted.get("has_invoice_cycle", None)
    has_approval_timeline = extracted.get("has_approval_timeline", None)

    remarks = []
    issues = []

    # Auto-detect if not explicitly extracted
    if has_invoice_cycle is None:
        invoice_patterns = [r"\d+\s*days?\s*(?:of|from|after)\s*(?:invoice|billing)", r"payment\s*within\s*\d+"]
        has_invoice_cycle = any(re.search(p, clause_text) for p in invoice_patterns)

    if has_approval_timeline is None:
        approval_patterns = [r"\d+\s*days?\s*(?:to|for)\s*(?:approv|comment|review)", r"deemed.*approv"]
        has_approval_timeline = any(re.search(p, clause_text) for p in approval_patterns)

    if not has_invoice_cycle:
        issues.append("No invoice-to-payment cycle specified")
        remarks.append(REMARKS["payment_no_invoice_cycle"])

    if not has_approval_timeline:
        issues.append("No deliverable approval timeline specified")
        remarks.append(REMARKS["payment_no_approval_timeline"])

    if issues:
        return RiskResult(
            clause_name="Payment Terms",
            risk_level="MEDIUM",
            risk_description="; ".join(issues),
            auto_remark=" ".join(remarks),
        )

    return RiskResult(
        clause_name="Payment Terms",
        risk_level="ACCEPTABLE",
        risk_description="Invoice cycle and approval timeline are specified.",
        auto_remark="",
    )


def evaluate_deliverables(extracted: dict) -> RiskResult:
    """
    Flags overlapping deliverables, unclear acceptance criteria, aggressive timelines,
    undocumented client dependencies.
    """
    flags = extracted.get("flags", [])
    issues_text = extracted.get("issues", "").lower()

    risk_phrases = {
        "overlap": "Overlapping deliverables",
        "unclear": "Unclear acceptance criteria",
        "aggressive": "Aggressive or impractical timelines",
        "client depend": "Client dependencies not documented",
        "no acceptance": "No acceptance criteria defined",
    }

    found_issues = []
    combined = " ".join(flags).lower() + " " + issues_text
    for kw, label in risk_phrases.items():
        if kw in combined:
            found_issues.append(label)

    if found_issues:
        return RiskResult(
            clause_name="Deliverables",
            risk_level="MEDIUM",
            risk_description="; ".join(found_issues),
            auto_remark=f"Issues identified with deliverables/timelines: {'; '.join(found_issues)}. ET to review and flag to client.",
        )

    return RiskResult(
        clause_name="Deliverables",
        risk_level="LOW",
        risk_description="No significant deliverable or timeline issues identified.",
        auto_remark="",
    )


def evaluate_personnel_replacement(extracted: dict) -> RiskResult:
    """
    Rules:
      - Replacement period ≤ 30 days OR absent → flag
    """
    clause_text = extracted.get("clause_text", "").lower()
    replacement_days = extracted.get("replacement_days", None)  # integer or None

    # Auto-detect if not extracted
    if replacement_days is None:
        m = re.search(r"replacement.*?(\d+)\s*days?", clause_text)
        if m:
            replacement_days = int(m.group(1))

    if replacement_days is None:
        return RiskResult(
            clause_name="Replacement/Substitution of Personnel",
            risk_level="MEDIUM",
            risk_description="No replacement period specified in the clause.",
            auto_remark=REMARKS["personnel_replacement_short"],
        )

    if replacement_days <= 30:
        return RiskResult(
            clause_name="Replacement/Substitution of Personnel",
            risk_level="MEDIUM",
            risk_description=f"Replacement period is only {replacement_days} days (≤ 30 days).",
            auto_remark=REMARKS["personnel_replacement_short"],
        )

    return RiskResult(
        clause_name="Replacement/Substitution of Personnel",
        risk_level="ACCEPTABLE",
        risk_description=f"Replacement period is {replacement_days} days (acceptable).",
        auto_remark="",
    )


def evaluate_liquidated_damages(extracted: dict) -> RiskResult:
    """
    Rules:
      - LD cap ≤ 10% → ACCEPTABLE
      - 10% < LD cap < 20% → MEDIUM
      - LD cap ≥ 20% or uncapped → HIGH
    """
    clause_text = extracted.get("clause_text", "")
    ld_cap_pct = extracted.get("ld_cap_percentage", None)

    if ld_cap_pct is None:
        ld_cap_pct = _parse_percentage(extracted.get("ld_cap_text", ""))

    if _is_uncapped(clause_text) or _is_uncapped(extracted.get("ld_cap_text", "")):
        return RiskResult(
            clause_name="Liquidated Damages",
            risk_level="HIGH",
            risk_description="LDs are uncapped. GTBL faces unlimited LD exposure.",
            auto_remark=REMARKS["ld_high"],
            needs_exception_approval=True,
            needs_eqcr=True,
        )

    if ld_cap_pct is None:
        return RiskResult(
            clause_name="Liquidated Damages",
            risk_level="NEEDS_REVIEW",
            risk_description="Could not determine LD cap percentage. Manual review required.",
            auto_remark=REMARKS["ld_medium"],
        )

    if ld_cap_pct <= 10:
        return RiskResult(
            clause_name="Liquidated Damages",
            risk_level="ACCEPTABLE",
            risk_description=f"LD cap is {ld_cap_pct}% (within GTBL's acceptable threshold of ≤10%).",
            auto_remark="",
        )
    elif ld_cap_pct < 20:
        return RiskResult(
            clause_name="Liquidated Damages",
            risk_level="MEDIUM",
            risk_description=f"LD cap is {ld_cap_pct}% (above 10% but below high-risk threshold of 20%).",
            auto_remark=REMARKS["ld_medium"],
        )
    else:
        return RiskResult(
            clause_name="Liquidated Damages",
            risk_level="HIGH",
            risk_description=f"LD cap is {ld_cap_pct}% (≥20%). This is HIGH RISK for GTBL.",
            auto_remark=REMARKS["ld_high"],
            needs_exception_approval=True,
            needs_eqcr=True,
        )


def evaluate_penalties(extracted: dict) -> RiskResult:
    """Same thresholds as LDs."""
    result = evaluate_liquidated_damages(extracted)
    result.clause_name = "Penalties"
    # Swap LD remarks for penalty remarks
    result.auto_remark = result.auto_remark.replace(
        REMARKS["ld_high"], REMARKS["penalties_high"]
    ).replace(
        REMARKS["ld_medium"], REMARKS["penalties_medium"]
    )
    if result.risk_level == "HIGH":
        result.auto_remark = REMARKS["penalties_high"]
    elif result.risk_level == "MEDIUM":
        result.auto_remark = REMARKS["penalties_medium"]
    return result


def evaluate_termination(extracted: dict) -> RiskResult:
    """
    Rules:
      - Termination rights are unilateral (only client can terminate) → HIGH
      - GTBL has symmetric termination rights → ACCEPTABLE
    """
    clause_text = extracted.get("clause_text", "").lower()
    gtbl_can_terminate = extracted.get("gtbl_can_terminate", None)
    is_unilateral = extracted.get("is_unilateral", None)

    # Auto-detect if not extracted
    if gtbl_can_terminate is None:
        gtbl_termination_phrases = [
            "consultant may terminate", "agency may terminate", "selected bidder may terminate",
            "bidder may terminate", "firm may terminate", "right to terminate"
        ]
        gtbl_can_terminate = any(p in clause_text for p in gtbl_termination_phrases)

    if is_unilateral is None:
        is_unilateral = not gtbl_can_terminate

    if is_unilateral:
        return RiskResult(
            clause_name="Termination Rights",
            risk_level="HIGH",
            risk_description="Termination rights appear to be unilateral (only client can terminate). GTBL has no termination right.",
            auto_remark=REMARKS["termination_unilateral"],
            needs_exception_approval=True,
        )

    return RiskResult(
        clause_name="Termination Rights",
        risk_level="ACCEPTABLE",
        risk_description="GTBL has symmetric termination rights.",
        auto_remark="",
    )


def evaluate_eligibility(extracted: dict) -> RiskResult:
    """
    Rules:
      - Any absolute declaration about blacklisting/debarment → HIGH (given GTBL history)
      - Any declaration about termination/penalty → HIGH
      - No-deviation bid + above conditions → HIGH with extra flag
    """
    clause_text = extracted.get("clause_text", "").lower()
    declaration_type = extracted.get("declaration_type", "").lower()  # "blacklisting" / "termination" / "penalty"
    is_no_deviation = extracted.get("is_no_deviation", False)
    uses_historical_language = extracted.get("uses_historical_language", None)

    # Detect "has not been" / "have not been" language
    historical_patterns = [r"has not been", r"have not been", r"has never been", r"was not"]
    if uses_historical_language is None:
        uses_historical_language = any(re.search(p, clause_text) for p in historical_patterns)

    blacklisting_keywords = ["blacklist", "debar", "sanction", "unblemished", "abandon"]
    termination_keywords = ["terminated", "termination for default", "termination for breach"]
    penalty_keywords = ["penali", "penalty for non-performance", "non-performance"]

    is_blacklisting = any(k in clause_text or k in declaration_type for k in blacklisting_keywords)
    is_termination = any(k in clause_text or k in declaration_type for k in termination_keywords)
    is_penalty = any(k in clause_text or k in declaration_type for k in penalty_keywords)

    remarks_parts = []
    issues = []

    if is_blacklisting:
        issues.append("Blacklisting/debarment declaration conflicts with GTBL's history (Oct 2021–Sept 2024)")
        remarks_parts.append(REMARKS["eligibility_blacklisting"])

    if is_termination or is_penalty:
        issues.append("Termination/penalty declaration conflicts with GTBL's current position")
        remarks_parts.append(REMARKS["eligibility_termination_penalty"])

    if is_no_deviation and (is_blacklisting or is_termination or is_penalty):
        issues.append("No-deviation clause detected — modifications may disqualify the bid")
        remarks_parts.append(REMARKS["no_deviation_bid"])

    # Suggest language modification if historical "has not been" language used
    deviation_suggestion = ""
    if uses_historical_language and (is_blacklisting or is_termination):
        deviation_suggestion = (
            "Suggested modification: Replace 'has not been' / 'have not been' with 'is not' to make "
            "the declaration as on the date of submission. Minimum changes only."
        )

    if issues:
        return RiskResult(
            clause_name="Eligibility Clause",
            risk_level="HIGH",
            risk_description="; ".join(issues),
            auto_remark="\n".join(remarks_parts),
            needs_exception_approval=True,
            deviation_suggested=deviation_suggestion,
        )

    return RiskResult(
        clause_name="Eligibility Clause",
        risk_level="LOW",
        risk_description="No eligibility conflicts identified with GTBL's current position.",
        auto_remark="",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────────────

EVALUATORS = {
    "liability": evaluate_liability,
    "insurance": evaluate_insurance,
    "scope": evaluate_scope,
    "payment": evaluate_payment_terms,
    "deliverables": evaluate_deliverables,
    "personnel": evaluate_personnel_replacement,
    "ld": evaluate_liquidated_damages,
    "penalties": evaluate_penalties,
    "termination": evaluate_termination,
    "eligibility": evaluate_eligibility,
}


def evaluate_clause(clause_type: str, extracted_data: dict) -> RiskResult:
    """
    Evaluate risk for a given clause type using extracted LLM data.
    clause_type must be one of the keys in EVALUATORS.
    """
    evaluator = EVALUATORS.get(clause_type)
    if not evaluator:
        raise ValueError(f"Unknown clause type: '{clause_type}'. Must be one of: {list(EVALUATORS.keys())}")
    return evaluator(extracted_data)
