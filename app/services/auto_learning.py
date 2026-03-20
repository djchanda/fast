"""
Auto-learning service.
When a reviewer marks a finding as false_positive, this service learns the
pattern and suppresses similar findings in future runs.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def suppress_false_positives(
    findings: Dict[str, List], project_id: int, form_id: int = None
) -> Dict[str, List]:
    """
    Filter out findings that match known false positive patterns for this project.

    Args:
        findings: The result_json findings dict from runner.
        project_id: Current project ID.
        form_id: Optional form ID for form-scoped suppression.

    Returns:
        Filtered findings dict with false positives removed.
    """
    from app.models.false_positive import FalsePositive

    patterns = FalsePositive.query.filter_by(
        project_id=project_id, is_active=True
    ).all()

    # Restrict to form-scoped + project-wide
    if form_id:
        patterns = [p for p in patterns if p.form_id is None or p.form_id == form_id]
    else:
        patterns = [p for p in patterns if p.form_id is None]

    if not patterns:
        return findings

    categories = [
        "spelling_errors", "format_issues", "value_mismatches",
        "missing_content", "extra_content", "layout_anomalies",
        "compliance_issues", "visual_mismatches",
    ]

    from app.extensions import db
    suppressed_total = 0

    result = dict(findings)
    for cat in categories:
        items = result.get(cat, [])
        filtered = []
        for item in items:
            desc = str(item.get("description", "")) + " " + str(item.get("text", ""))
            matched = False
            for fp in patterns:
                if fp.category and fp.category != cat:
                    continue
                pattern = fp.pattern or ""
                if fp.match_mode == "exact":
                    matched = pattern.lower() == desc.lower()
                elif fp.match_mode == "regex":
                    try:
                        matched = bool(re.search(pattern, desc, re.IGNORECASE))
                    except re.error:
                        pass
                else:  # contains
                    matched = pattern.lower() in desc.lower()

                if matched:
                    fp.suppressed_count = (fp.suppressed_count or 0) + 1
                    suppressed_total += 1
                    break

            if not matched:
                filtered.append(item)

        result[cat] = filtered

    if suppressed_total:
        try:
            db.session.commit()
        except Exception:
            pass

    return result


def learn_false_positive(
    finding_review_id: int,
    project_id: int,
    created_by: str,
) -> bool:
    """
    Create a FalsePositive pattern from a resolved finding_review marked as false_positive.

    Returns True if pattern was created.
    """
    from app.models.finding_review import FindingReview
    from app.models.false_positive import FalsePositive
    from app.extensions import db

    review = FindingReview.query.get(finding_review_id)
    if not review or review.status != "false_positive":
        return False

    # Extract a pattern from the finding description (first 60 chars)
    description = review.finding_description or ""
    pattern = description[:80].strip()
    if not pattern:
        return False

    # Check if same pattern already exists
    existing = FalsePositive.query.filter_by(
        project_id=project_id,
        category=review.finding_category,
        pattern=pattern,
    ).first()

    if existing:
        existing.is_active = True
        db.session.commit()
        return True

    fp = FalsePositive(
        project_id=project_id,
        category=review.finding_category,
        pattern=pattern,
        match_mode="contains",
        form_id=None,
        created_by=created_by,
        is_active=True,
    )
    db.session.add(fp)
    db.session.commit()
    return True
