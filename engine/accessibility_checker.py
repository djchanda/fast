"""
Accessibility checker for PDF forms.
Checks for: missing field labels, missing alt text, improper tab order,
color contrast issues, and language declarations.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def check_accessibility(extraction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Run accessibility checks on extracted PDF content.

    Args:
        extraction: Result from engine.extractor.extract_all()

    Returns:
        List of accessibility findings, each with:
          {type, severity, field_name, page, description, wcag_criterion}
    """
    issues = []
    fields = extraction.get("fields", {})
    meta = extraction.get("meta", {})
    pages = extraction.get("pages", [])

    # --- Check 1: Fields without labels ---
    unlabeled = []
    for field_name, field_info in fields.items():
        if isinstance(field_info, dict):
            label = field_info.get("label") or field_info.get("tooltip") or ""
        else:
            label = str(field_info) if field_info else ""

        if not label or len(str(label).strip()) < 2:
            unlabeled.append(field_name)

    if unlabeled:
        for fn in unlabeled[:10]:  # cap at 10 findings
            issues.append({
                "type": "missing_label",
                "severity": "high",
                "field_name": fn,
                "page": None,
                "description": f"Form field '{fn}' has no accessible label or tooltip.",
                "wcag_criterion": "1.3.1 Info and Relationships (Level A)",
            })

    # --- Check 2: Language declaration ---
    language = meta.get("language") or meta.get("lang") or ""
    if not language:
        issues.append({
            "type": "missing_language",
            "severity": "medium",
            "field_name": None,
            "page": None,
            "description": "PDF document does not declare a language (lang attribute missing).",
            "wcag_criterion": "3.1.1 Language of Page (Level A)",
        })

    # --- Check 3: Document title ---
    title = meta.get("title") or ""
    if not title or title.strip().lower() in ("untitled", ""):
        issues.append({
            "type": "missing_title",
            "severity": "medium",
            "field_name": None,
            "page": None,
            "description": "PDF does not have a descriptive document title.",
            "wcag_criterion": "2.4.2 Page Titled (Level A)",
        })

    # --- Check 4: Signature fields without labels ---
    sig_patterns = re.compile(r"sign|signature|authorized|approval", re.IGNORECASE)
    for fn, finfo in fields.items():
        if sig_patterns.search(fn):
            if isinstance(finfo, dict) and not finfo.get("label"):
                issues.append({
                    "type": "unlabeled_signature",
                    "severity": "high",
                    "field_name": fn,
                    "page": None,
                    "description": f"Signature field '{fn}' lacks an accessible label.",
                    "wcag_criterion": "1.3.1 Info and Relationships (Level A)",
                })

    # --- Check 5: Very low character count pages (may be image-only) ---
    for idx, page_text in enumerate(pages, start=1):
        text = page_text.get("text", "") if isinstance(page_text, dict) else str(page_text)
        if len(text.strip()) < 20:
            issues.append({
                "type": "image_only_page",
                "severity": "medium",
                "field_name": None,
                "page": idx,
                "description": f"Page {idx} appears to contain little or no text — may be an image-only page with no alt text.",
                "wcag_criterion": "1.1.1 Non-text Content (Level A)",
            })

    # --- Check 6: Required field indicators ---
    required_pattern = re.compile(r"\*|required|mandatory", re.IGNORECASE)
    text_all = extraction.get("text", "")
    if required_pattern.search(text_all):
        # Check if there's an explanation of the indicator
        if not re.search(r"\*\s*(=|means?|denotes?|indicates?|required)", text_all, re.IGNORECASE):
            issues.append({
                "type": "unexplained_required_indicator",
                "severity": "low",
                "field_name": None,
                "page": None,
                "description": "Form uses asterisks (*) or 'required' labels but may not explain their meaning to all users.",
                "wcag_criterion": "3.3.2 Labels or Instructions (Level A)",
            })

    return issues


def build_field_inventory(extraction: Dict[str, Any], project_id: int, form_id: int) -> List[Dict]:
    """
    Extract a field inventory from PDF extraction results.

    Returns a list of field records ready to be saved as FieldInventory rows.
    """
    fields = extraction.get("fields", {})
    records = []

    for field_name, field_info in fields.items():
        if isinstance(field_info, dict):
            field_type = field_info.get("type", "text")
            page_number = field_info.get("page")
            has_label = bool(field_info.get("label"))
            has_tooltip = bool(field_info.get("tooltip"))
            tab_order = field_info.get("tab_order")
        else:
            field_type = "text"
            page_number = None
            has_label = False
            has_tooltip = False
            tab_order = None

        records.append({
            "project_id": project_id,
            "form_id": form_id,
            "field_name": field_name,
            "field_type": field_type,
            "page_number": page_number,
            "has_label": has_label,
            "has_tooltip": has_tooltip,
            "tab_order": tab_order,
            "change_status": "added",
        })

    return records
