from typing import Optional, Literal, Dict, Any, List

ValidationMode = Literal["basic", "specific", "benchmark"]


def _format_visual_diffs_for_llm(visual_diffs: list) -> str:
    """Format visual diff rows into a readable summary for the LLM."""
    if not visual_diffs:
        return "No visual diff data available."
    lines = []
    for v in visual_diffs:
        page = v.get("page", "?")
        sim = v.get("similarity", 1.0)
        diff_pct = v.get("diff_pixels_pct", 0.0)
        op = v.get("alignment_op", "matched")
        sig_candidate = v.get("signature_candidate", False)
        sig_label = v.get("signature_label", "")
        zone = v.get("zone_analysis") or {}
        changed_zones = zone.get("changed_zones", [])
        change_pattern = zone.get("change_pattern", "")
        change_hint = zone.get("change_hint", "")

        if op == "deleted":
            lines.append(
                f"  Page {page}: DELETED — entire page removed from current PDF."
            )
            continue
        if op == "inserted":
            is_blank = v.get("is_blank_page", False)
            if is_blank:
                lines.append(
                    f"  Page {page}: INSERTED (is_blank_page=true) — blank/empty page added in current PDF. "
                    f"Likely an intentional separator. Low severity."
                )
            else:
                lines.append(
                    f"  Page {page}: INSERTED (is_blank_page=false) — new content page in current PDF with no baseline counterpart."
                )
            continue

        added_texts = v.get("added_texts", [])
        removed_texts = v.get("removed_texts", [])
        has_text_changes = v.get("has_text_changes", False)
        formatting_summary = v.get("formatting_summary", "")
        has_fmt = v.get("has_formatting_changes", False)

        status = "MAJOR" if v.get("major") else ("WARN" if v.get("warn") else "OK")
        parts = [f"  Page {page}: {status} | similarity={sim:.3f} ({diff_pct:.1f}% pixels differ)"]

        # ── Content diff ──────────────────────────────────────────────────
        if not has_text_changes and not has_fmt and (v.get("major") or v.get("warn")):
            parts.append("TEXT DIFF: Content identical — pixel differences are rendering/watermark noise only")
        else:
            if added_texts:
                parts.append("ADDED TEXT: " + ", ".join(repr(t) for t in added_texts[:8]))
            if removed_texts:
                parts.append("REMOVED TEXT: " + ", ".join(repr(t) for t in removed_texts[:8]))

        # ── Formatting diff (granular) ────────────────────────────────────
        bold_changed = v.get("bold_changed", [])
        size_changed = v.get("size_changed", [])
        list_align = v.get("list_alignment_shifted", [])
        if bold_changed:
            parts.append("BOLD CHANGED: " + ", ".join(repr(t) for t in bold_changed[:6]))
        if size_changed:
            parts.append("FONT SIZE CHANGED: " + ", ".join(size_changed[:6]))
        if list_align:
            parts.append("LIST ALIGNMENT SHIFTED: " + ", ".join(repr(t) for t in list_align[:6]))
        elif formatting_summary and not bold_changed and not size_changed and not list_align:
            parts.append(f"FORMATTING CHANGES: {formatting_summary}")

        if sig_candidate:
            parts.append(f"[SIGNATURE CANDIDATE near '{sig_label}']")

        lines.append(" | ".join(parts))
    return "\n".join(lines)


def build_prompt(
    mode: ValidationMode,
    current_doc: Dict[str, Any],
    benchmark_doc: Optional[Dict[str, Any]],
    base_prompt: str,
) -> List[dict]:
    """
    Returns messages list suitable for OpenAI-style chat format.

    Notes:
    - Supports scanned PDFs via OCR text in current_doc["text"] and current_doc["pages"].
    - Includes visual diff evidence if present.
    - Keeps STRICT JSON output requirement.
    """

    current_text = current_doc.get("text", "")
    current_fields = current_doc.get("fields", {})
    current_pages = current_doc.get("pages", [])
    current_meta = current_doc.get("meta", {})
    current_visual_diffs = current_doc.get("visual_diffs", [])

    benchmark_text = ""
    benchmark_fields = {}
    benchmark_pages = []
    benchmark_meta = {}
    benchmark_visual_diffs = []

    if benchmark_doc:
        benchmark_text = benchmark_doc.get("text", "")
        benchmark_fields = benchmark_doc.get("fields", {})
        benchmark_pages = benchmark_doc.get("pages", [])
        benchmark_meta = benchmark_doc.get("meta", {})
        benchmark_visual_diffs = benchmark_doc.get("visual_diffs", [])

    system_msg = {
        "role": "system",
        "content": (
            "You are an expert PDF form validator.\n"
            "You ALWAYS return STRICT JSON with the schema requested.\n"
            "Do not include any commentary outside JSON.\n\n"
            "General rules:\n"
            "- Some PDFs are scanned/printed forms; extracted text may come from OCR.\n"
            "- OCR text can contain noise (spacing, minor typos). Use best judgment.\n"
            "- Prefer form fields where available; otherwise rely on extracted text.\n"
            "- If per-page text is provided, you MUST use it for correct page numbers.\n\n"
            "WATERMARK / SPECIMEN ARTIFACT DETECTION (critical for accuracy):\n"
            "- Insurance and legal forms frequently carry diagonal 'SAMPLE', 'SPECIMEN', 'DRAFT', "
            "or 'VOID' watermark stamps.\n"
            "- When OCR text contains scattered isolated single characters throughout a page "
            "(e.g., individual letters like 'n e m i c e p S', 'S p e c i m e n', or any "
            "short sequence that spells a watermark word), this is an OCR watermark artifact — "
            "NOT a typo, spelling error, or data entry issue.\n"
            "- Do NOT report scattered single-character OCR noise as spelling_errors or format_issues.\n"
            "- If a watermark artifact pattern is detected, report it ONCE per run as a single "
            "format_issue with severity='low' and description like: "
            "'SPECIMEN/watermark stamp detected in OCR output — not a real content error.'\n"
            "- Do NOT create one finding per page for the same watermark artifact.\n"
            "- Similarly, if the same artifact pattern repeats identically across all pages "
            "(e.g., same scattered characters on every page), it is a single document-level "
            "artefact — report once, not per page.\n\n"
            "FORMATTING CHANGES (bold / font-size / alignment):\n"
            "- BOLD CHANGED: text that flipped between bold and non-bold. Report each term as a "
            "format_issue. Severity: 'medium' if it is a heading, section title, or legal keyword; "
            "'low' if it is running body text.\n"
            "- FONT SIZE CHANGED: text whose point size changed. Report as format_issue if the change "
            "is meaningful (e.g. a heading shrank, a clause became smaller). Severity: 'medium' for "
            "headings/titles, 'low' for body text.\n"
            "- LIST ALIGNMENT SHIFTED: list markers like (a), (b), 1., i. that moved horizontally. "
            "These are ALWAYS medium-severity layout_anomaly findings in legal/insurance documents "
            "because they indicate structural indentation changes. Report each shifted marker with "
            "its surrounding context.\n"
            "- Body-text alignment shifts (≥3 words): report as low-severity layout_anomaly.\n"
            "- Do NOT conflate formatting changes with content changes — report them in separate "
            "format_issues / layout_anomalies entries.\n\n"
            "TEXT DIFF — the authoritative accuracy signal (read this first):\n"
            "- Each visual diff row now includes TEXT DIFF: which compares the actual words extracted "
            "from both PDFs at the page level.\n"
            "- If TEXT DIFF says 'Content identical' or 'Text content identical': "
            "there are NO real content differences on that page. "
            "Do NOT report any spelling_errors, value_mismatches, or missing_content for that page. "
            "Any visual pixel differences are purely rendering/watermark noise. "
            "At most, note once as a low-severity layout_anomaly only if it seems significant.\n"
            "- If TEXT DIFF shows ADDED TEXT: those are words/values that appear in the current PDF "
            "but not in the baseline — these are real changes. Report what was added and where.\n"
            "- If TEXT DIFF shows REMOVED TEXT: those are words/values present in the baseline but "
            "missing from the current — these are real changes. Report what is missing.\n"
            "- ADDED TEXT containing dates, names, dollar amounts, or policy numbers = field values "
            "were populated — report as value_mismatches or format_issues as appropriate.\n\n"
            "TESTER PERSPECTIVE — how a real form validator reads results:\n"
            "- A real tester cares about business defects: wrong values, missing clauses, missing signatures.\n"
            "- Minor layout shifts (a line slightly left or right) are NOT defects unless they affect readability.\n"
            "- Blank pages added between sections are often intentional separators — NOT critical defects.\n"
            "- Rendering differences (DPI, font hinting, watermarks) are noise — do NOT report them.\n"
            "- Only report alignment changes when they are visually obvious and affect ≥ 3 text elements.\n\n"
            "VALUE CHANGE FORMAT (critical rule):\n"
            "- Whenever a value has changed, you MUST show it as: 'Changed from \"OLD VALUE\" to \"NEW VALUE\"'.\n"
            "- Use REMOVED TEXT as the old value and ADDED TEXT as the new value when they are related.\n"
            "- Example: if REMOVED TEXT has '$1,000' and ADDED TEXT has '$2,000', report: "
            "\"Coverage limit changed from \\\"$1,000\\\" to \\\"$2,000\\\".\"\n"
            "- For value_mismatches, always populate 'expected' with the old value and 'actual' with the new value.\n\n"
            "BLANK / EMPTY INSERTED PAGES:\n"
            "- If alignment_op='inserted' AND is_blank_page=true: this is a blank separator page.\n"
            "  Report it as a single low-severity extra_content item with description: "
            "'Blank page inserted — likely an intentional separator/placeholder. Validation continues from next page.'\n"
            "  Do NOT mark it critical. Do NOT block validation.\n"
            "- If alignment_op='inserted' AND is_blank_page=false: this is a real content addition. "
            "Report as medium or high extra_content depending on significance.\n\n"
            "Visual diff interpretation rules:\n"
            "- If signature_candidate=true, always report as missing_content or visual_mismatches "
            "with category 'Signature / approval block'.\n"
            "- alignment_op='deleted': page removed from actual PDF — always critical missing_content.\n"
            "- alignment_op='inserted' with is_blank_page=true: low-severity blank separator — see above.\n"
            "- alignment_op='inserted' with is_blank_page=false: real inserted page — report accordingly.\n"
            "- NEVER write 'Significant visual difference detected (Similarity: X.XXX)' as a finding. "
            "Always describe the specific text that changed based on the TEXT DIFF data.\n\n"
            "Deterministic reporting rules:\n"
            "1) Populate every top-level list even if empty.\n"
            "2) summary_counts must equal the exact lengths of the arrays.\n"
            "3) pages_impacted must be the sorted unique set of page numbers from all findings.\n"
            "4) top_findings must contain up to 5 strongest findings.\n"
            "5) overall_summary must be based only on the findings you output.\n"
            "6) Never say 'no differences' if any issue list has items.\n"
        ),
    }

    base_schema = r"""
Return ONLY valid JSON with this structure:

{
  "mode": "<basic|specific|benchmark>",

  "spelling_errors": [
    {"page": 1, "severity": "low", "text": "orig word", "suggestion": "correct word", "description": "brief note"}
  ],

  "format_issues": [
    {"page": 1, "severity": "low", "field_name": "date", "description": "Date is not in YYYY-MM-DD", "snippet": "12/05/25"}
  ],

  "value_mismatches": [
    {"page": 1, "severity": "critical", "field_name": "premium_amount", "expected": "100.00", "actual": "90.00", "description": "Value changed"}
  ],

  "missing_content": [
    {"page": 3, "severity": "high", "field_name": "president_signature", "description": "President signature missing"}
  ],

  "extra_content": [
    {"page": 2, "severity": "medium", "field_name": "unexpected_section", "description": "Unexpected section present"}
  ],

  "layout_anomalies": [
    {"page": 2, "severity": "medium", "description": "Layout shifted or misaligned"}
  ],

  "visual_mismatches": [
    {
      "page": 3,
      "severity": "high",
      "category": "Signature / approval block",
      "description": "Possible missing or changed signature near PRESIDENT",
      "evidence": "Visual diff says signature_candidate=true near PRESIDENT"
    }
  ],

  "summary_counts": {
    "spelling_errors": 0,
    "format_issues": 0,
    "value_mismatches": 0,
    "missing_content": 0,
    "extra_content": 0,
    "layout_anomalies": 0,
    "visual_mismatches": 0,
    "total": 0
  },

  "pages_impacted": [1,2,3],

  "top_findings": [
    {"severity": "critical", "page": 4, "category": "Value", "short": "Minimum premium changed from 1000 to 0"}
  ],

  "overall_summary": "Found <total> issue(s) across <n> page(s).",
  "accuracy_score": 0,

  "compliance_issues": [
    {"page": 1, "severity": "high", "standard": "WCAG21", "requirement": "1.1.1", "description": "Image missing alt text"}
  ],

  "accessibility_issues": [
    {"page": 1, "severity": "medium", "type": "missing_label", "field_name": "field_x", "description": "Form field has no accessible label"}
  ],

  "confidence_scores": {
    "spelling_errors": 0.95,
    "format_issues": 0.90,
    "value_mismatches": 0.98,
    "missing_content": 0.85,
    "extra_content": 0.80,
    "layout_anomalies": 0.75,
    "visual_mismatches": 0.92,
    "overall": 0.88
  }
}
"""

    summary_enforcement = """
Before writing the final JSON:
- Compute summary_counts from the exact lengths of the arrays.
- Build pages_impacted from all page numbers found in the findings.
- Build top_findings using the strongest findings first.
- Write overall_summary LAST.
"""

    if mode == "basic":
        user_content = f"""
Perform BASIC validation on the following PDF.

Scope:
- Check spelling and obvious typos.
- Check simple formatting issues (dates, currency, capitalization).
- Identify obviously missing standard fields based only on document content.
- There is NO benchmark document in this mode.

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF visual diffs (optional):
{current_visual_diffs}

{summary_enforcement}

{base_schema}
"""
    elif mode == "specific":
        user_content = f"""
Perform SPECIFIC validation according to the user's rules.

User-provided validation instructions:
{base_prompt}

Apply those rules to the following PDF.

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF visual diffs (optional):
{current_visual_diffs}

{summary_enforcement}

{base_schema}
"""
    else:
        formatted_visual_diffs = _format_visual_diffs_for_llm(current_visual_diffs)
        user_content = f"""
Perform BENCHMARK validation comparing a GOLDEN benchmark PDF and a CURRENT PDF.

Benchmark rules:
- Compare page by page using per-page text.
- Use benchmark fields as expected values when present.
- Use the visual diff summary below as evidence, guided by the change_pattern hints.
- Your job is to identify actual actionable differences, not rendering noise.
- Focus on real business changes: field values populated/changed, signatures, declarations,
  premium values, coverage limits, content updates, added/removed sections.
- For pages where change_pattern='page_wide' and the text shows no meaningful difference,
  do NOT add a finding — this is rendering noise.
- For pages where change_pattern='page_wide' but the text DOES show differences,
  describe the specific text change, not the visual similarity score.

User-provided additional instructions:
{base_prompt}

--- BASELINE (GOLDEN) PDF ---
Extraction meta: {benchmark_meta}
Full text:
{benchmark_text}

Per-page text:
{benchmark_pages}

Form fields: {benchmark_fields}

--- CURRENT PDF ---
Extraction meta: {current_meta}
Full text:
{current_text}

Per-page text:
{current_pages}

Form fields: {current_fields}

--- VISUAL DIFF SUMMARY (page-by-page, with zone analysis) ---
{formatted_visual_diffs}

{summary_enforcement}

{base_schema}
"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]