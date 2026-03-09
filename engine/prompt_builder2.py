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
    """

    current_text = current_doc.get("text", "")
    current_fields = current_doc.get("fields", {})
    current_pages = current_doc.get("pages", [])
    current_meta = current_doc.get("meta", {})
    current_visual_inventory = current_doc.get("page_visual_inventory", [])
    current_visual_diffs = current_doc.get("visual_diffs", [])

    benchmark_text = ""
    benchmark_fields = {}
    benchmark_pages = []
    benchmark_meta = {}
    benchmark_visual_inventory = []
    benchmark_visual_diffs = []

    if benchmark_doc:
        benchmark_text = benchmark_doc.get("text", "")
        benchmark_fields = benchmark_doc.get("fields", {})
        benchmark_pages = benchmark_doc.get("pages", [])
        benchmark_meta = benchmark_doc.get("meta", {})
        benchmark_visual_inventory = benchmark_doc.get("page_visual_inventory", [])
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
            "- If per-page text is provided, you MUST use it to assign correct page numbers.\n"
            "- If visual diff evidence is provided, you MUST use it and MUST NOT ignore warned or major visual pages.\n\n"
            "Deterministic reporting rules (MANDATORY):\n"
            "1) Populate all arrays even if empty.\n"
            "2) summary_counts MUST exactly match the lengths of the output arrays.\n"
            "3) pages_impacted MUST be the sorted unique set of page numbers from all findings.\n"
            "4) top_findings MUST include up to 5 strongest findings.\n"
            "5) overall_summary MUST be derived ONLY from the arrays in the JSON output.\n"
            "6) Never say 'no differences' or 'no issues' if any issue array has entries.\n"
            "7) In benchmark mode, if any visual_diffs row has warn=true or major=true, you MUST either:\n"
            "   - create a visual_mismatches entry for that page, OR\n"
            "   - create another issue on that page that clearly explains the visual difference.\n"
            "8) Use these severity levels only: critical, high, medium, low.\n"
        ),
    }

    base_schema = r"""
Return ONLY valid JSON with this structure (no markdown, no extra prose):

{
  "mode": "<basic|specific|benchmark>",

  "spelling_errors": [
    {"page": 1, "severity": "low", "text": "orig word", "suggestion": "correct word", "evidence": "context snippet"}
  ],

  "format_issues": [
    {"page": 1, "severity": "low", "description": "Date is not in YYYY-MM-DD", "snippet": "12/05/25", "evidence": "context snippet"}
  ],

  "value_mismatches": [
    {"page": 1, "severity": "critical", "field_name": "premium_amount", "expected": "100.00", "actual": "90.00", "evidence": "context snippet"}
  ],

  "missing_content": [
    {"page": 3, "severity": "high", "field_name": "", "description": "Signature section missing", "evidence": "what is missing"}
  ],

  "extra_content": [
    {"page": 2, "severity": "medium", "field_name": "", "description": "Unexpected fee section", "evidence": "what is extra"}
  ],

  "layout_anomalies": [
    {"page": 2, "severity": "medium", "category": "layout_shift", "description": "Table header misaligned or moved", "evidence": "layout evidence"}
  ],

  "visual_mismatches": [
    {
      "page": 3,
      "severity": "high",
      "category": "signature_change",
      "description": "A visual element appears missing/added/changed.",
      "evidence": {
        "source": "visual_diffs|inference",
        "details": "short explanation",
        "diff_pixels_pct": 0.0,
        "regions": [{"bbox": [0,0,0,0], "area_pct": 0.0}]
      }
    }
  ],

  "compliance_issues": [
    {"page": 1, "severity": "medium", "rule": "custom rule", "description": "rule failed", "evidence": "context"}
  ],

  "summary_counts": {
    "spelling_errors": 0,
    "format_issues": 0,
    "value_mismatches": 0,
    "missing_content": 0,
    "extra_content": 0,
    "layout_anomalies": 0,
    "visual_mismatches": 0,
    "compliance_issues": 0,
    "total": 0
  },

  "pages_impacted": [1,2,3],

  "top_findings": [
    {"severity": "critical", "page": 4, "category": "value_mismatches", "short": "Minimum premium changed from 1000 to 0"}
  ],

  "overall_summary": "Found <total> issues across <n> pages. Critical/high: ... Counts: spelling=X, format=Y, value=Z, missing=A, extra=B, layout=C, visual=D, compliance=E.",

  "accuracy_score": 0
}
"""

    summary_enforcement = """
Before writing final JSON:
- Compute summary_counts from array lengths.
- Compute pages_impacted from unique page numbers in all issue arrays.
- Compute top_findings from the strongest findings first.
- Write overall_summary LAST using only the computed counts and top findings.
"""

    if mode == "basic":
        user_content = f"""
Perform BASIC validation on the following PDF.

Scope:
- Check spelling and obvious typos.
- Check simple formatting issues (dates, currency, capitalization).
- Identify obviously missing standard fields based only on the content.
- There is NO benchmark document in this mode.

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF page visual inventory:
{current_visual_inventory}

{summary_enforcement}

{base_schema}
"""

    elif mode == "specific":
        user_content = f"""
Perform SPECIFIC validation according to the user's rules.

User-provided validation instructions:
{base_prompt}

Apply these rules to the following PDF.

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF page visual inventory:
{current_visual_inventory}

{summary_enforcement}

{base_schema}
"""

    else:
        user_content = f"""
Perform BENCHMARK validation comparing a GOLDEN (benchmark) PDF and a CURRENT PDF.

Benchmark rules:
- Benchmark PDF is the expected template, allowing minor OCR noise.
- Compare page-by-page.
- Use both text evidence and visual evidence.
- Call out every meaningful difference with page, category, severity, and evidence.

Additional user instructions:
{base_prompt}

Benchmark PDF extraction meta:
{benchmark_meta}

Benchmark PDF text:
{benchmark_text}

Benchmark PDF per-page text:
{benchmark_pages}

Benchmark PDF form fields:
{benchmark_fields}

Benchmark PDF page visual inventory:
{benchmark_visual_inventory}

Benchmark PDF visual diffs:
{benchmark_visual_diffs}

Current PDF extraction meta:
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text:
{current_pages}

Current PDF form fields:
{current_fields}

Current PDF page visual inventory:
{current_visual_inventory}

Current PDF visual diffs:
{current_visual_diffs}

Interpretation guidance:
- Use value_mismatches for data/field/value changes.
- Use missing_content or extra_content for sections/blocks appearing or disappearing.
- Use layout_anomalies for moved/misaligned structural changes.
- Use visual_mismatches for signatures, stamps, logos, images, or changes that are primarily visual.
- If visual diffs show warn/major on a page and there is no text explanation, create a visual_mismatches item.
- If a page has both a value change and a visual change, report both if both matter.

{summary_enforcement}

{base_schema}
"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]