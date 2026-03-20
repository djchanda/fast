"""
Natural Language Test Case Builder.

Accepts a plain-English description like:
  "Check that all signature fields are present and dated, and that the form
   number in the header matches the document footer."

Returns a structured prompt_text suitable for saving to TestCase.prompt_text,
plus a suggested mode ("specific" or "benchmark").
"""
from __future__ import annotations

import json
import os
from typing import Dict, Any

from engine.llm_client import run_validation


_BUILDER_SYSTEM_PROMPT = """\
You are a form testing assistant. The user will describe in plain English what they want to
validate in a PDF form. Your job is to convert that description into a structured, machine-readable
test specification.

Return a JSON object with exactly these fields:
{
  "mode": "basic" | "specific" | "benchmark",
  "prompt_text": "<structured test rules as a numbered list, ready to paste as a test case prompt>",
  "validation_rules": [
    {"rule": "<concise rule description>", "category": "<spelling_errors|format_issues|value_mismatches|missing_content|extra_content|compliance_issues>", "severity": "error|warning"}
  ],
  "suggested_name": "<a short 3-7 word test case name>",
  "rationale": "<why you chose this mode and these rules>"
}

Rules for choosing mode:
- If the user mentions comparing to a reference/golden copy: mode = "benchmark"
- If the user describes specific business rules or field checks: mode = "specific"
- If the description is general validation (spelling, formatting): mode = "basic"

Make the prompt_text actionable and concrete — it should be directly usable as input to a form
validation LLM without further modification. Write it as a numbered list of clear instructions.
"""


def build_testcase_from_nl(description: str, provider: str = None) -> Dict[str, Any]:
    """
    Convert a plain-English description into a structured test case spec.

    Args:
        description: The user's plain-English description of what to test.
        provider: LLM provider override (defaults to LLM_PROVIDER env var).

    Returns:
        dict with keys: mode, prompt_text, validation_rules, suggested_name, rationale, error (if any)
    """
    if not description or not description.strip():
        return {"error": "Description is required."}

    messages = [
        {"role": "system", "content": _BUILDER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Convert this test description into a structured test case:\n\n{description}"},
    ]

    result = run_validation(messages, provider=provider)

    if result.get("error"):
        return {"error": result["error"], "details": result.get("details", "")}

    # Ensure required fields have defaults
    result.setdefault("mode", "specific")
    result.setdefault("prompt_text", description)
    result.setdefault("validation_rules", [])
    result.setdefault("suggested_name", description[:60])
    result.setdefault("rationale", "")

    return result
