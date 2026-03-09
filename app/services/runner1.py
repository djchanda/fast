# app/services/runner.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from flask import current_app

from app.models.form import Form
from app.models.test_case import TestCase

from engine.extractor import extract_all
from engine.prompt_builder import build_prompt
from engine.llm_client import run_validation

from app.reporting.html_report import write_cli_style_report


# -----------------------
# Helpers
# -----------------------
def _forms_dir(project_id: int) -> str:
    return os.path.join(current_app.instance_path, "uploads", f"project_{project_id}", "forms")


def _pdf_abs_path(project_id: int, stored_filename: str) -> str:
    return os.path.join(_forms_dir(project_id), stored_filename)


def _read_pdf_bytes(project_id: int, stored_filename: str) -> bytes:
    path = _pdf_abs_path(project_id, stored_filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"PDF not found on disk: {path}")
    with open(path, "rb") as f:
        return f.read()


def _count_list(v: Any) -> int:
    if not v:
        return 0
    if isinstance(v, list):
        return len(v)
    return 1


def _ensure_schema_defaults(result_json: Any, mode: str) -> Dict[str, Any]:
    """
    Hardens the output so reporting never crashes if LLM returns partial/odd JSON.
    """
    base: Dict[str, Any] = {
        "mode": mode,
        "spelling_errors": [],
        "format_issues": [],
        "value_mismatches": [],
        "missing_content": [],
        "extra_content": [],
        "layout_anomalies": [],
        "compliance_issues": [],
        "overall_summary": "",
        "accuracy_score": 0,
        "visual_validation": [],
    }

    if not isinstance(result_json, dict):
        return base

    base.update(result_json)

    # Ensure all list fields exist
    for k in [
        "spelling_errors",
        "format_issues",
        "value_mismatches",
        "missing_content",
        "extra_content",
        "layout_anomalies",
        "compliance_issues",
        "visual_validation",
    ]:
        if base.get(k) is None:
            base[k] = []
        # if a single dict/string slips in, normalize to list to keep report safe
        if k in base and not isinstance(base[k], list):
            base[k] = [base[k]]

    return base


def _derive_metrics(result_json: Dict[str, Any]) -> tuple[int, int, int]:
    """
    Simple, stable scoring:
    - Errors: spelling/format/value/missing/compliance + major visual
    - Warnings: extra/layout + minor visual
    """
    errors = (
        _count_list(result_json.get("spelling_errors"))
        + _count_list(result_json.get("format_issues"))
        + _count_list(result_json.get("value_mismatches"))
        + _count_list(result_json.get("missing_content"))
        + _count_list(result_json.get("compliance_issues"))
    )
    warnings = (
        _count_list(result_json.get("extra_content"))
        + _count_list(result_json.get("layout_anomalies"))
    )

    visual = result_json.get("visual_validation") or []
    if isinstance(visual, list):
        if any(isinstance(v, dict) and v.get("major") for v in visual):
            errors += 1
        elif any(isinstance(v, dict) and v.get("warn") for v in visual):
            warnings += 1

    passed = 1 if errors == 0 else 0
    return errors, warnings, passed


# -----------------------
# Runner
# -----------------------
def run_testcase(*, project_id: int, tc: TestCase, run_id: int, rr_id: int) -> dict:
    """
    Runs one testcase and returns payload for DB + report generation.

    IMPORTANT: This function MUST NOT crash the caller.
    It returns an error-shaped result_json if anything fails.
    """

    # Always provide report function so UI can generate report even on errors
    write_fn = write_cli_style_report

    try:
        # -------- Validate inputs --------
        if not tc.form_id:
            result_json = _ensure_schema_defaults({"error": "Test case has no form selected."}, tc.mode or "basic")
            return {
                "result_json": result_json,
                "summary_text": "Overall Assessment: FAIL\nTest case has no form selected.",
                "errors": 1,
                "warnings": 0,
                "passed": 0,
                "main_form": None,
                "bench_form": None,
                "write_report_fn": write_fn,
            }

        main_form = Form.query.get(tc.form_id)
        if not main_form:
            result_json = _ensure_schema_defaults({"error": "Selected form not found in DB."}, tc.mode or "basic")
            return {
                "result_json": result_json,
                "summary_text": "Overall Assessment: FAIL\nSelected form not found in DB.",
                "errors": 1,
                "warnings": 0,
                "passed": 0,
                "main_form": None,
                "bench_form": None,
                "write_report_fn": write_fn,
            }

        mode = (tc.mode or "basic").strip().lower()
        rules_text = (tc.prompt_text or "").strip()

        # CLI behavior: specific mode without rules -> fall back to basic
        effective_mode = mode
        if effective_mode == "specific" and not rules_text:
            effective_mode = "basic"

        bench_form: Optional[Form] = None
        benchmark_doc = None

        # CLI behavior: benchmark requires benchmark form
        if effective_mode == "benchmark":
            if not tc.benchmark_form_id:
                result_json = _ensure_schema_defaults(
                    {"error": "Benchmark mode requires a benchmark (golden copy) form."},
                    effective_mode,
                )
                return {
                    "result_json": result_json,
                    "summary_text": "Overall Assessment: FAIL\nBenchmark mode requires a benchmark (golden copy) form.",
                    "errors": 1,
                    "warnings": 0,
                    "passed": 0,
                    "main_form": main_form,
                    "bench_form": None,
                    "write_report_fn": write_fn,
                }

            bench_form = Form.query.get(tc.benchmark_form_id)
            if not bench_form:
                result_json = _ensure_schema_defaults({"error": "Benchmark form not found in DB."}, effective_mode)
                return {
                    "result_json": result_json,
                    "summary_text": "Overall Assessment: FAIL\nBenchmark form not found in DB.",
                    "errors": 1,
                    "warnings": 0,
                    "passed": 0,
                    "main_form": main_form,
                    "bench_form": None,
                    "write_report_fn": write_fn,
                }

        # -------- Extract documents --------
        current_bytes = _read_pdf_bytes(project_id, main_form.stored_filename)
        current_doc = extract_all(current_bytes)

        if effective_mode == "benchmark" and bench_form:
            bench_bytes = _read_pdf_bytes(project_id, bench_form.stored_filename)
            benchmark_doc = extract_all(bench_bytes)

        # -------- LLM validation --------
        messages = build_prompt(
            mode=effective_mode,
            current_doc=current_doc,
            benchmark_doc=benchmark_doc,
            base_prompt=rules_text,
        )

        llm_out = run_validation(messages)
        result_json = _ensure_schema_defaults(llm_out, effective_mode)

        # -------- Visual diffs (benchmark only) --------
        # IMPORTANT: vd is created inside this block ONLY.
        # Nothing outside this block references 'vd'.
        result_json["visual_validation"] = []

        if effective_mode == "benchmark" and bench_form:
            try:
                from engine.visual_diff import VisualDiff  # local import avoids import-time crashes
                vd = VisualDiff(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))

                # compare_pdfs_detailed must return list[dict] with page/similarity/major/warn/note/snapshot_path
                visual = vd.compare_pdfs_detailed(
                    original_pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    expected_pdf_path=_pdf_abs_path(project_id, bench_form.stored_filename),
                    result_id=f"run{run_id}_rr{rr_id}",
                )
                result_json["visual_validation"] = visual or []
            except Exception as e:
                # Do NOT fail the run; surface in report as a warning-like finding
                result_json["visual_validation"] = []
                result_json.setdefault("extra_content", [])
                result_json["extra_content"].append(
                    {"description": f"Visual diff generation failed: {str(e)}", "page": ""}
                )

        # -------- Summary + metrics --------
        # Keep it simple + stable for UI
        overall = (result_json.get("overall_summary") or "").strip()
        if result_json.get("error"):
            summary_text = f"Overall Assessment: FAIL\n{result_json.get('error')}"
        elif overall:
            # if summary contains FAIL wording, keep FAIL label
            verdict = "FAIL" if "fail" in overall.lower() else "PASS"
            summary_text = f"Overall Assessment: {verdict}\n{overall}"
        else:
            summary_text = "Overall Assessment: Completed."

        errors, warnings, passed = _derive_metrics(result_json)

        return {
            "result_json": result_json,
            "summary_text": summary_text,
            "errors": errors,
            "warnings": warnings,
            "passed": passed,
            "main_form": main_form,
            "bench_form": bench_form,
            "write_report_fn": write_fn,
        }

    except Exception as e:
        # Last-resort guard: NEVER crash the caller
        mode = (getattr(tc, "mode", None) or "basic")
        result_json = _ensure_schema_defaults({"error": f"Runner crashed: {str(e)}"}, mode)
        return {
            "result_json": result_json,
            "summary_text": f"Overall Assessment: FAIL\nRunner crashed: {str(e)}",
            "errors": 1,
            "warnings": 0,
            "passed": 0,
            "main_form": None,
            "bench_form": None,
            "write_report_fn": write_fn,
        }
