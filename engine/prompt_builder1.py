# prompt_builder.py
from typing import Optional, Literal, Dict, Any

ValidationMode = Literal["basic", "specific", "benchmark"]


def build_prompt(
    mode: ValidationMode,
    current_doc: Dict[str, Any],
    benchmark_doc: Optional[Dict[str, Any]],
    base_prompt: str,
) -> list[dict]:
    """
    Returns messages list suitable for OpenAI-style chat format.

    Notes:
    - Supports scanned PDFs via OCR text in current_doc["text"] and current_doc["pages"].
    - Keeps STRICT JSON output requirement (engine expects JSON).
    """

    current_text = current_doc.get("text", "")
    current_fields = current_doc.get("fields", {})
    current_pages = current_doc.get("pages", [])
    current_meta = current_doc.get("meta", {})

    benchmark_text = ""
    benchmark_fields = {}
    benchmark_pages = []
    benchmark_meta = {}
    if benchmark_doc:
        benchmark_text = benchmark_doc.get("text", "")
        benchmark_fields = benchmark_doc.get("fields", {})
        benchmark_pages = benchmark_doc.get("pages", [])
        benchmark_meta = benchmark_doc.get("meta", {})

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
        ),
    }

    base_schema = """
Return ONLY valid JSON with this structure:

{
  "mode": "<basic|specific|benchmark>",
  "spelling_errors": [
    {"page": 1, "text": "orig word", "suggestion": "correct word"}
  ],
  "format_issues": [
    {"page": 1, "description": "Date is not in YYYY-MM-DD", "snippet": "12/05/25"}
  ],
  "value_mismatches": [
    {"field_name": "premium_amount", "expected": "100.00", "actual": "90.00"}
  ],
  "missing_content": [
    {"description": "Signature section missing", "page": 3}
  ],
  "extra_content": [
    {"description": "Unexpected fee section", "page": 2}
  ],
  "layout_anomalies": [
    {"description": "Table header misaligned", "page": 2}
  ],
  "overall_summary": "Short human-readable summary.",
  "accuracy_score": 0
}
"""

    if mode == "basic":
        user_content = f"""
Perform BASIC validation on the following PDF:

- Check spelling & obvious typos.
- Check simple formatting issues (dates, currency, capitalization).
- Identify any obviously missing standard fields (e.g., name, date, signature) based ONLY on the content.
- There is NO benchmark document in this mode.

Current PDF extraction meta (OCR may be used):
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text (may be OCR):
{current_pages}

Current PDF form fields (if any):
{current_fields}

{base_schema}
"""
    elif mode == "specific":
        user_content = f"""
Perform SPECIFIC validation according to the user's rules.

User-provided validation instructions:
{base_prompt}

Apply these rules to the following PDF:

Current PDF extraction meta (OCR may be used):
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text (may be OCR):
{current_pages}

Current PDF form fields (if any):
{current_fields}

{base_schema}
"""
    else:  # benchmark
        user_content = f"""
Perform BENCHMARK validation comparing a GOLDEN (benchmark) PDF and a CURRENT PDF.

Benchmarks:
- Benchmark PDF text should be treated as the correct template (allowing minor OCR noise if benchmark used OCR).
- Benchmark form fields are expected values/structure when present.
- Identify semantic differences, missing sections, extra sections, value mismatches, and layout anomalies based on content.

User-provided additional instructions (optional):
{base_prompt}

Benchmark PDF extraction meta (OCR may be used):
{benchmark_meta}

Benchmark PDF text:
{benchmark_text}

Benchmark PDF per-page text (may be OCR):
{benchmark_pages}

Benchmark PDF form fields (if any):
{benchmark_fields}

Current PDF extraction meta (OCR may be used):
{current_meta}

Current PDF text:
{current_text}

Current PDF per-page text (may be OCR):
{current_pages}

Current PDF form fields (if any):
{current_fields}

{base_schema}
"""

    return [
        system_msg,
        {"role": "user", "content": user_content},
    ]
