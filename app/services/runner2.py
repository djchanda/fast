from __future__ import annotations

import os
from typing import Any, Dict, Optional, List, Set

from flask import current_app

from app.models.form import Form
from app.models.test_case import TestCase

from engine.extractor import extract_all
from engine.prompt_builder import build_prompt
from engine.llm_client import run_validation

from app.reporting.html_report import write_cli_style_report


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


def _safe_page(v: Any) -> Optional[int]:
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except Exception:
        return None


def _severity_rank(sev: str) -> int:
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return order.get((sev or "").strip().lower(), 1)


def _default_severity_for_category(category: str) -> str:
    mapping = {
        "spelling_errors": "low",
        "format_issues": "low",
        "value_mismatches": "critical",
        "missing_content": "high",
        "extra_content": "medium",
        "layout_anomalies": "medium",
        "visual_mismatches": "high",
        "compliance_issues": "high",
    }
    return mapping.get(category, "medium")


def _ensure_list(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _ensure_schema_defaults(result_json: Any, mode: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "mode": mode,
        "spelling_errors": [],
        "format_issues": [],
        "value_mismatches": [],
        "missing_content": [],
        "extra_content": [],
        "layout_anomalies": [],
        "visual_mismatches": [],
        "compliance_issues": [],
        "summary_counts": {},
        "pages_impacted": [],
        "top_findings": [],
        "overall_summary": "",
        "accuracy_score": 0,
        "visual_validation": [],
    }

    if not isinstance(result_json, dict):
        return base

    base.update(result_json)

    for k in [
        "spelling_errors",
        "format_issues",
        "value_mismatches",
        "missing_content",
        "extra_content",
        "layout_anomalies",
        "visual_mismatches",
        "compliance_issues",
        "visual_validation",
        "top_findings",
        "pages_impacted",
    ]:
        if base.get(k) is None:
            base[k] = []
        if k != "pages_impacted" and k != "top_findings" and not isinstance(base[k], list):
            base[k] = [base[k]]

    if not isinstance(base.get("summary_counts"), dict):
        base["summary_counts"] = {}

    return base


def _all_issue_categories() -> List[str]:
    return [
        "spelling_errors",
        "format_issues",
        "value_mismatches",
        "missing_content",
        "extra_content",
        "layout_anomalies",
        "visual_mismatches",
        "compliance_issues",
    ]


def _pages_with_any_issue(result_json: Dict[str, Any]) -> Set[int]:
    pages: Set[int] = set()
    for cat in _all_issue_categories():
        for item in _ensure_list(result_json.get(cat)):
            if isinstance(item, dict):
                p = _safe_page(item.get("page"))
                if p is not None:
                    pages.add(p)
    return pages


def _finding_short(category: str, item: Dict[str, Any]) -> str:
    if category == "value_mismatches":
        field = item.get("field_name") or item.get("field") or "field"
        expected = item.get("expected", "")
        actual = item.get("actual", "")
        return f"{field} changed from {expected} to {actual}".strip()

    if category in ("missing_content", "extra_content", "layout_anomalies", "visual_mismatches", "compliance_issues"):
        return str(item.get("description") or item.get("rule") or item.get("category") or category)

    if category == "spelling_errors":
        return f"Spelling: {item.get('text', '')} -> {item.get('suggestion', '')}".strip()

    if category == "format_issues":
        return str(item.get("description") or item.get("snippet") or "Format issue")

    return str(item)


def _normalize_issue_items(result_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make sure all issue rows are shaped consistently.
    """
    for category in _all_issue_categories():
        normalized = []
        for item in _ensure_list(result_json.get(category)):
            if not isinstance(item, dict):
                item = {"description": str(item)}
            item.setdefault("severity", _default_severity_for_category(category))
            if "page" in item:
                p = _safe_page(item.get("page"))
                item["page"] = p if p is not None else item.get("page")
            normalized.append(item)
        result_json[category] = normalized
    return result_json


def _reconcile_visual_findings(result_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    If visual engine detected warn/major pages and LLM gave no page-level explanation,
    inject a fallback visual_mismatches finding.
    """
    visual_rows = _ensure_list(result_json.get("visual_validation"))
    pages_with_issues = _pages_with_any_issue(result_json)
    visual_mismatches = _ensure_list(result_json.get("visual_mismatches"))

    for row in visual_rows:
        if not isinstance(row, dict):
            continue
        page = _safe_page(row.get("page"))
        if page is None:
            continue

        if not row.get("warn") and not row.get("major"):
            continue

        if page in pages_with_issues:
            continue

        severity = "high" if row.get("major") else "medium"
        visual_mismatches.append(
            {
                "page": page,
                "severity": severity,
                "category": row.get("category_hint") or "unclassified_visual_change",
                "description": "Visual difference detected by image comparison but not clearly classified from text evidence.",
                "evidence": {
                    "source": "visual_engine",
                    "details": row.get("note", ""),
                    "diff_pixels_pct": row.get("diff_pixels_pct", 0.0),
                    "regions": row.get("diff_regions", []),
                    "snapshot_path": row.get("snapshot_path", ""),
                },
            }
        )

    result_json["visual_mismatches"] = visual_mismatches
    return result_json


def _recompute_summary(result_json: Dict[str, Any]) -> Dict[str, Any]:
    counts = {}
    pages = sorted(_pages_with_any_issue(result_json))

    total = 0
    all_findings = []

    for category in _all_issue_categories():
        items = _ensure_list(result_json.get(category))
        counts[category] = len(items)
        total += len(items)

        for item in items:
            if not isinstance(item, dict):
                continue
            sev = item.get("severity", _default_severity_for_category(category))
            page = _safe_page(item.get("page"))
            all_findings.append(
                {
                    "severity": sev,
                    "page": page,
                    "category": category,
                    "short": _finding_short(category, item),
                }
            )

    counts["total"] = total
    result_json["summary_counts"] = counts
    result_json["pages_impacted"] = pages

    all_findings.sort(
        key=lambda x: (
            -_severity_rank(x.get("severity", "")),
            x.get("page") if x.get("page") is not None else 999999,
            x.get("category", ""),
        )
    )
    result_json["top_findings"] = all_findings[:5]

    if total == 0:
        result_json["overall_summary"] = "Found 0 issues across 0 pages. Counts: spelling=0, format=0, value=0, missing=0, extra=0, layout=0, visual=0, compliance=0."
    else:
        top3 = "; ".join(
            f"p{f['page']}: {f['short']}" if f.get("page") is not None else f["short"]
            for f in result_json["top_findings"][:3]
        )
        result_json["overall_summary"] = (
            f"Found {total} issues across {len(pages)} pages. "
            f"Top findings: {top3}. "
            f"Counts: spelling={counts['spelling_errors']}, "
            f"format={counts['format_issues']}, "
            f"value={counts['value_mismatches']}, "
            f"missing={counts['missing_content']}, "
            f"extra={counts['extra_content']}, "
            f"layout={counts['layout_anomalies']}, "
            f"visual={counts['visual_mismatches']}, "
            f"compliance={counts['compliance_issues']}."
        )

    return result_json


def _derive_metrics(result_json: Dict[str, Any]) -> tuple[int, int, int]:
    errors = 0
    warnings = 0

    for category in _all_issue_categories():
        for item in _ensure_list(result_json.get(category)):
            if not isinstance(item, dict):
                errors += 1
                continue
            sev = (item.get("severity") or _default_severity_for_category(category)).lower()
            if sev in ("critical", "high"):
                errors += 1
            else:
                warnings += 1

    passed = 1 if errors == 0 else 0
    return errors, warnings, passed


def run_testcase(*, project_id: int, tc: TestCase, run_id: int, rr_id: int) -> dict:
    write_fn = write_cli_style_report

    try:
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
        effective_mode = mode

        if effective_mode == "specific" and not rules_text:
            effective_mode = "basic"

        bench_form: Optional[Form] = None
        benchmark_doc = None

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

        current_bytes = _read_pdf_bytes(project_id, main_form.stored_filename)
        current_doc = extract_all(current_bytes)

        visual_rows = []
        if effective_mode == "benchmark" and bench_form:
            bench_bytes = _read_pdf_bytes(project_id, bench_form.stored_filename)
            benchmark_doc = extract_all(bench_bytes)

            try:
                from engine.visual_diff import VisualDiff

                vd = VisualDiff(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))
                visual_rows = vd.compare_pdfs_detailed(
                    original_pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    expected_pdf_path=_pdf_abs_path(project_id, bench_form.stored_filename),
                    result_id=f"run{run_id}_rr{rr_id}",
                )
            except Exception as e:
                visual_rows = [
                    {
                        "page": "",
                        "similarity": "",
                        "diff_pixels_pct": 0.0,
                        "major": False,
                        "warn": True,
                        "note": f"Visual diff generation failed: {str(e)}",
                        "category_hint": "visual_engine_error",
                        "region_count": 0,
                        "diff_regions": [],
                        "snapshot_path": "",
                    }
                ]

            current_doc["visual_diffs"] = visual_rows
            if benchmark_doc is not None:
                benchmark_doc["visual_diffs"] = []

        messages = build_prompt(
            mode=effective_mode,
            current_doc=current_doc,
            benchmark_doc=benchmark_doc,
            base_prompt=rules_text,
        )

        llm_out = run_validation(messages)
        result_json = _ensure_schema_defaults(llm_out, effective_mode)
        result_json["visual_validation"] = visual_rows

        result_json = _normalize_issue_items(result_json)
        result_json = _reconcile_visual_findings(result_json)
        result_json = _recompute_summary(result_json)

        if result_json.get("error"):
            summary_text = f"Overall Assessment: FAIL\n{result_json.get('error')}"
        else:
            verdict = "FAIL" if _count_list(result_json.get("top_findings")) and any(
                _severity_rank(f.get("severity", "")) >= 3 for f in result_json.get("top_findings", [])
            ) else "PASS"
            summary_text = f"Overall Assessment: {verdict}\n{result_json.get('overall_summary', '').strip()}"

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