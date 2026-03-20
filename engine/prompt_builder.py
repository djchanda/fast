from typing import Optional, Literal, Dict, Any, List

ValidationMode = Literal["basic", "specific", "benchmark"]


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
            "Important:\n"
            "- Some PDFs are scanned/printed forms; extracted text may come from OCR.\n"
            "- OCR text can contain noise (spacing, minor typos). Use best judgment.\n"
            "- Prefer form fields where available; otherwise rely on extracted text.\n"
            "- If per-page text is provided, you MUST use it for correct page numbers.\n"
            "- If visual_diffs are provided, you MUST use them as evidence.\n"
            "- Never ignore a page only because similarity is still high. A page can be materially different even at 99.800% similarity.\n"
            "- If a visual diff row has signature_candidate=true, you MUST report that page as a likely signature or approval-block issue.\n"
            "- If a visual diff row has warn=true or major=true and similarity <= 0.9985, you MUST classify that page unless there is strong evidence it is harmless formatting noise.\n"
            "- visual_diff rows may contain an 'alignment_op' field:\n"
            "    'matched'  = pages were correctly paired by the sequence-alignment engine.\n"
            "    'deleted'  = this page exists in the expected PDF but is MISSING from the actual PDF (page was removed). Always report as a critical missing_content finding.\n"
            "    'inserted' = this page exists in the actual PDF but has NO counterpart in the expected PDF (extra page added). Always report as a critical extra_content finding.\n"
            "- When alignment_op is 'deleted' or 'inserted', expected_page_num and actual_page_num in the diff row tell you the exact page numbers involved.\n\n"
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
        user_content = f"""
Perform BENCHMARK validation comparing a GOLDEN benchmark PDF and a CURRENT PDF.

Benchmark rules:
- Compare page by page using per-page text.
- Use benchmark fields as expected values when present.
- Use visual_diffs as evidence for visual differences.
- If visual_diffs show signature_candidate=true on a page, report that page in missing_content and/or visual_mismatches.
- If visual_diffs show warn=true or major=true and similarity <= 0.9985, do not ignore that page unless it is clearly harmless formatting noise.
- Your job is to identify actual actionable differences, not noise.
- Focus on real business changes: signatures, declarations, premium values, coverage limits, content updates, added/removed sections.

User-provided additional instructions:
{base_prompt}

Benchmark PDF extraction meta:
{benchmark_meta}

Benchmark PDF text:
{benchmark_text}

Benchmark PDF per-page text:
{benchmark_pages}

Benchmark PDF form fields:
{benchmark_fields}

Benchmark PDF visual diffs (optional):
{benchmark_visual_diffs}

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF visual diffs:
{current_visual_diffs}

{summary_enforcement}

{base_schema}
"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]