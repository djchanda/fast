from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from flask import current_app

from app.models.form import Form
from app.models.test_case import TestCase

from engine.extractor import extract_all
from engine.prompt_builder import build_prompt
from engine.llm_client import run_validation

from app.reporting.html_report import write_cli_style_report

logger = logging.getLogger(__name__)


VISUAL_REVIEW_SIMILARITY_THRESHOLD = 0.9985  # 99.85%
VISUAL_SIGNATURE_SIMILARITY_THRESHOLD = 0.9990  # 99.90%


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
    base: Dict[str, Any] = {
        "mode": mode,
        "spelling_errors": [],
        "format_issues": [],
        "value_mismatches": [],
        "missing_content": [],
        "extra_content": [],
        "layout_anomalies": [],
        "typography_issues": [],
        "structural_changes": [],
        "compliance_issues": [],
        "visual_mismatches": [],
        "accessibility_issues": [],
        "summary_counts": {},
        "pages_impacted": [],
        "top_findings": [],
        "overall_summary": "",
        "accuracy_score": 0,
        "visual_validation": [],
        "error": "",
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
        "typography_issues",
        "structural_changes",
        "compliance_issues",
        "visual_mismatches",
        "accessibility_issues",
        "pages_impacted",
        "top_findings",
        "visual_validation",
    ]:
        if base.get(k) is None:
            base[k] = []
        if k in base and not isinstance(base[k], list):
            base[k] = [base[k]]

    if not isinstance(base.get("summary_counts"), dict):
        base["summary_counts"] = {}

    return base


def _contains_signature_issue(page_items: list[dict]) -> bool:
    keywords = ("signature", "president", "secretary", "approval", "signed")
    blob = " ".join(
        str(x) for item in page_items for x in item.values() if not isinstance(x, (dict, list))
    ).lower()
    return any(k in blob for k in keywords)


def _page_has_real_business_issue(page_items: list[dict]) -> bool:
    strong_buckets = {
        "value_mismatches",
        "missing_content",
        "extra_content",
        "compliance_issues",
    }

    for item in page_items:
        if not isinstance(item, dict):
            continue
        bucket = item.get("_bucket")
        if bucket in strong_buckets:
            return True
    return False


def _index_existing_items_by_page(result_json: Dict[str, Any]) -> Dict[int, list[dict]]:
    existing_by_page: Dict[int, list[dict]] = {}

    for bucket in [
        "value_mismatches",
        "missing_content",
        "extra_content",
        "layout_anomalies",
        "visual_mismatches",
        "format_issues",
        "spelling_errors",
        "compliance_issues",
    ]:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                p = it.get("page")
                try:
                    p = int(str(p).strip()) if p not in (None, "") else None
                except Exception:
                    p = None
                if p is not None:
                    cloned = dict(it)
                    cloned["_bucket"] = bucket
                    existing_by_page.setdefault(p, []).append(cloned)

    return existing_by_page


def _safe_similarity(v: dict) -> float:
    sim = v.get("similarity")
    try:
        return float(sim)
    except Exception:
        return 1.0


def _reconcile_visual_findings(result_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Precision-first deterministic reconciliation.

    - Converts visual diff rows into missing_content / extra_content / visual_mismatches.
    - Converts field structure changes (added/removed form fields) into findings.
    - Signature candidates below the tighter threshold become missing_content.
    """
    visual = result_json.get("visual_validation") or []
    if not isinstance(visual, list):
        visual = []

    # Pull and remove transient _field_diff injected by run_testcase
    field_diff = result_json.pop("_field_diff", {}) or {}

    existing_by_page = _index_existing_items_by_page(result_json)
    result_json.setdefault("visual_mismatches", [])
    result_json.setdefault("missing_content", [])
    result_json.setdefault("extra_content", [])
    result_json.setdefault("structural_changes", [])

    # ── Field structure changes (removed / added AcroForm fields) ──────────
    for fname in field_diff.get("removed_fields", []):
        result_json["missing_content"].append({
            "page": None,
            "severity": "critical",
            "field_name": fname,
            "category": "Structural change",
            "description": f"Form field '{fname}' was removed from the document.",
            "evidence": "Detected by AcroForm field structure comparison.",
        })
        result_json["structural_changes"].append({
            "page": None,
            "severity": "critical",
            "element_type": "field",
            "change_type": "removed",
            "element_name": fname,
            "description": f"Form field '{fname}' was removed.",
        })

    for fname in field_diff.get("added_fields", []):
        result_json["extra_content"].append({
            "page": None,
            "severity": "high",
            "field_name": fname,
            "category": "Structural change",
            "description": f"New form field '{fname}' was added to the document.",
            "evidence": "Detected by AcroForm field structure comparison.",
        })
        result_json["structural_changes"].append({
            "page": None,
            "severity": "high",
            "element_type": "field",
            "change_type": "added",
            "element_name": fname,
            "description": f"Form field '{fname}' was added.",
        })

    for cf in field_diff.get("changed_fields", []):
        fname = cf.get("name", "")
        result_json["visual_mismatches"].append({
            "page": None,
            "severity": "high",
            "category": "Field property",
            "description": (
                f"Form field '{fname}' changed type from "
                f"'{cf.get('expected_type')}' to '{cf.get('actual_type')}'."
            ),
            "evidence": "Detected by AcroForm field structure comparison.",
        })

    # ── Collect page-count changes before the main loop ─────────────────────
    # The sequence-alignment algorithm marks some baseline pages as "deleted"
    # whenever the current PDF has fewer pages. This does NOT mean content was
    # removed — the same content may simply have been consolidated onto fewer
    # pages. We therefore emit ONE structural note about the page count change
    # rather than a per-page CRITICAL "page removed" finding which is almost
    # always misleading and causes false positives.
    deleted_pages = []
    inserted_pages = []
    all_exp_pages = [v.get("expected_page_num") or v.get("page") for v in visual
                     if isinstance(v, dict) and v.get("alignment_op", "matched") != "inserted"]
    all_act_pages = [v.get("actual_page_num") or v.get("page") for v in visual
                     if isinstance(v, dict) and v.get("alignment_op", "matched") != "deleted"]
    baseline_total = max((p for p in all_exp_pages if p), default=0)
    current_total  = max((p for p in all_act_pages if p), default=0)

    for v in visual:
        if not isinstance(v, dict):
            continue
        op = str(v.get("alignment_op") or "matched").lower()
        if op == "deleted":
            pg = v.get("expected_page_num") or v.get("page")
            if pg:
                deleted_pages.append(pg)
        elif op == "inserted":
            pg = v.get("actual_page_num") or v.get("page")
            if pg:
                inserted_pages.append(pg)

    if deleted_pages:
        pg_list = ", ".join(str(p) for p in sorted(deleted_pages))
        result_json["structural_changes"].append({
            "page": None,
            "severity": "high",
            "element_type": "page_count",
            "change_type": "reduced",
            "element_name": "page_count",
            "description": (
                f"Page count changed: baseline has {baseline_total} page(s), "
                f"current has {current_total} page(s). "
                f"Baseline page(s) {pg_list} have no direct counterpart in the current PDF — "
                f"content may have been consolidated, reformatted, or genuinely removed. "
                f"Review the matched pages for content differences."
            ),
        })

    if inserted_pages:
        for label_pg in inserted_pages:
            # Look up the original visual entry to get is_blank_page
            entry = next((v for v in visual if isinstance(v, dict)
                          and str(v.get("alignment_op") or "").lower() == "inserted"
                          and (v.get("actual_page_num") or v.get("page")) == label_pg), {})
            is_blank = entry.get("is_blank_page") is True
            if is_blank:
                result_json["extra_content"].append({
                    "page": label_pg,
                    "severity": "low",
                    "field_name": f"page_{label_pg}_blank",
                    "category": "Blank page inserted",
                    "description": (
                        f"Page {label_pg} of the current PDF is a blank page with no counterpart "
                        f"in the baseline — likely an intentional separator or placeholder."
                    ),
                    "evidence": entry.get("note") or "Blank page found in current PDF.",
                })
            else:
                result_json["extra_content"].append({
                    "page": label_pg,
                    "severity": "high",
                    "field_name": f"page_{label_pg}",
                    "category": "Inserted page",
                    "description": (
                        f"Page {label_pg} of the current PDF has no counterpart in the baseline "
                        f"— this is an extra / inserted page with content."
                    ),
                    "evidence": entry.get("note") or "Extra page found in current PDF.",
                })

    for v in visual:
        if not isinstance(v, dict):
            continue

        # ── Skip deleted / inserted — handled above ──────────────────────────
        alignment_op = str(v.get("alignment_op") or "matched").lower()
        if alignment_op in ("deleted", "inserted"):
            continue

        # ── Standard per-page comparison ────────────────────────────────────

        p = v.get("page")
        try:
            p = int(str(p).strip()) if p not in (None, "") else None
        except Exception:
            p = None

        if p is None:
            continue

        page_items = existing_by_page.get(p, [])
        already_signature_like = _contains_signature_issue(page_items)
        has_real_business_issue = _page_has_real_business_issue(page_items)
        sim = _safe_similarity(v)

        sig_conf = str(v.get("signature_confidence") or "none").lower()

        # Strong deterministic fallback for missing signature
        if (
            v.get("signature_candidate")
            and sig_conf in {"high", "medium"}
            and sim <= VISUAL_SIGNATURE_SIMILARITY_THRESHOLD
            and not already_signature_like
        ):
            label = v.get("signature_label") or "signature"
            reason = v.get("signature_reason") or f"Visual diff is near '{label}' in a signature zone."

            result_json["missing_content"].append(
                {
                    "page": p,
                    "severity": "high" if sig_conf == "high" else "medium",
                    "field_name": f"{str(label).lower()}_signature",
                    "category": "Signature / approval block",
                    "description": f"Possible missing or changed signature near {label}.",
                    "evidence": reason,
                }
            )
            existing_by_page.setdefault(p, []).append(
                {
                    "page": p,
                    "description": reason,
                    "_bucket": "missing_content",
                }
            )
            continue



        # Generic visual mismatch only when similarity is tighter and page has no stronger finding
        if (
            (v.get("major") or v.get("warn"))
            and sim <= VISUAL_REVIEW_SIMILARITY_THRESHOLD
            and not page_items
            and not has_real_business_issue
        ):
            zone = v.get("zone_analysis") or {}
            change_pattern = zone.get("change_pattern", "")
            change_hint = zone.get("change_hint", "")
            changed_zones = zone.get("changed_zones", [])

            has_text_changes = v.get("has_text_changes", False)
            has_fmt_changes = v.get("has_formatting_changes", False)
            formatting_summary = v.get("formatting_summary", "")

            # If only alignment shifts (no text changes, no bold) — skip entirely, too noisy
            if (
                not has_text_changes
                and has_fmt_changes
                and formatting_summary
                and "bold" not in formatting_summary.lower()
                and "indented" in formatting_summary.lower()
            ):
                continue

            # Page-wide diff at this similarity almost always means rendering/font noise
            if change_pattern == "page_wide":
                severity = "low"
                description = (
                    f"Page-wide rendering difference (similarity={sim:.3f}, "
                    f"{v.get('diff_pixels_pct', 0):.1f}% pixels differ). "
                    "Changes are spread across the entire page, which typically indicates "
                    "a font rendering, watermark, or DPI difference rather than a content change. "
                    "Review the visual snapshot to confirm."
                )
            elif change_pattern == "header_only":
                severity = "medium"
                description = (
                    f"Header area changed (similarity={sim:.3f}). "
                    "Check policy number, effective date, named insured, or logo in the page header."
                )
            elif change_pattern == "footer_area":
                severity = "high"
                description = (
                    f"Footer/signature area changed (similarity={sim:.3f}). "
                    "Check signature blocks, footer dates, and footer text."
                )
            elif changed_zones:
                severity = "medium" if v.get("warn") else "high"
                description = (
                    f"Visual difference in {', '.join(changed_zones)} "
                    f"(similarity={sim:.3f}). {change_hint}"
                ).strip()
            else:
                severity = "medium" if v.get("warn") else "high"
                description = v.get("note") or f"Visual difference detected (similarity={sim:.3f})."

            result_json["visual_mismatches"].append(
                {
                    "page": p,
                    "severity": severity,
                    "category": "Visual difference",
                    "description": description,
                    "evidence": (
                        f"similarity={v.get('similarity')}, "
                        f"diff_pixels_pct={v.get('diff_pixels_pct')}, "
                        f"zones={', '.join(changed_zones) if changed_zones else 'unknown'}"
                    ),
                }
            )
            existing_by_page.setdefault(p, []).append(
                {
                    "page": p,
                    "description": description,
                    "_bucket": "visual_mismatches",
                }
            )
    return result_json


def _refresh_summary_fields(result_json: Dict[str, Any]) -> Dict[str, Any]:
    buckets = [
        "spelling_errors",
        "format_issues",
        "value_mismatches",
        "missing_content",
        "extra_content",
        "layout_anomalies",
        "typography_issues",
        "structural_changes",
        "visual_mismatches",
    ]

    counts = {k: len(result_json.get(k, []) or []) for k in buckets}
    counts["total"] = sum(counts.values())
    result_json["summary_counts"] = counts

    pages = set()
    for bucket in buckets + ["compliance_issues", "accessibility_issues"]:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                p = it.get("page")
                try:
                    if p not in (None, ""):
                        pages.add(int(str(p).strip()))
                except Exception:
                    pass

    result_json["pages_impacted"] = sorted(pages)

    top_findings = []
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    merged = []
    for bucket in buckets:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                merged.append(
                    {
                        "severity": str(it.get("severity") or "medium").lower(),
                        "page": it.get("page", ""),
                        "category": it.get("category") or bucket.replace("_", " ").title(),
                        "short": it.get("description") or it.get("field_name") or bucket,
                    }
                )
    merged.sort(key=lambda x: severity_order.get(x["severity"], 9))
    result_json["top_findings"] = merged[:5]

    if not result_json.get("overall_summary"):
        total = counts["total"]
        page_count = len(result_json["pages_impacted"])
        top = "; ".join(x["short"] for x in result_json["top_findings"][:3]) if result_json["top_findings"] else "none"
        result_json["overall_summary"] = (
            f"Found {total} issue(s) across {page_count} page(s). Top findings: {top}."
        )

    return result_json


def _reconcile_benchmark_visual(result_json: Dict[str, Any]) -> Dict[str, Any]:
    """For benchmark runs: add visual_mismatch entries for pages with >=2% pixel
    diff that the LLM did not produce a finding for (missed detections, not FP-suppressed items)."""
    visual = result_json.get("visual_validation") or []
    # Check ALL finding buckets — a page already covered by missing_content,
    # structural_changes, value_mismatches, etc. must not also get a spurious
    # "not classified by LLM" visual_mismatch entry.
    all_buckets = [
        "visual_mismatches", "missing_content", "extra_content",
        "value_mismatches", "format_issues", "layout_anomalies",
        "structural_changes", "spelling_errors", "typography_issues",
    ]
    existing_pages = {
        item.get("page")
        for bucket in all_buckets
        for item in (result_json.get(bucket) or [])
        if isinstance(item, dict) and item.get("page") is not None
    }
    added = []
    for entry in visual:
        if not entry.get("major"):
            continue
        # Deleted/inserted pages are already handled by _reconcile_visual_findings
        op = str(entry.get("alignment_op") or "matched").lower()
        if op in ("deleted", "inserted"):
            continue
        page = entry.get("page") or entry.get("actual_page_num")
        if page is None or page in existing_pages:
            continue
        diff_pct = entry.get("diff_pixels_pct") or entry.get("diff_pct") or 0.0
        added.append({
            "page": page,
            "severity": "medium",
            "description": (
                f"Significant visual difference detected on page {page} "
                f"({diff_pct:.1f}% pixel diff) — not classified by LLM."
            ),
            "category": "Visual Mismatch",
            "source": "reconciler",
        })
        existing_pages.add(page)
    if added:
        result_json["visual_mismatches"] = list(result_json.get("visual_mismatches") or []) + added
    return result_json


def _derive_metrics(result_json: Dict[str, Any]) -> tuple[int, int, int]:
    errors = (
        _count_list(result_json.get("spelling_errors"))
        + _count_list(result_json.get("format_issues"))
        + _count_list(result_json.get("value_mismatches"))
        + _count_list(result_json.get("missing_content"))
        + _count_list(result_json.get("compliance_issues"))
        + _count_list(result_json.get("visual_mismatches"))
        + _count_list(result_json.get("structural_changes"))
    )
    warnings = (
        _count_list(result_json.get("extra_content"))
        + _count_list(result_json.get("layout_anomalies"))
        + _count_list(result_json.get("typography_issues"))
        + _count_list(result_json.get("accessibility_issues"))
    )

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

        visual = []
        # Basic/specific mode: render pages as single-panel snapshots so the
        # HTML report can show the form in the Snapshot column without a diff.
        if effective_mode in ("basic", "specific"):
            try:
                from engine.visual_diff import VisualDiff
                vd_basic = VisualDiff(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))
                visual = vd_basic.render_pages(
                    pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    result_id=f"run{run_id}_rr{rr_id}",
                ) or []
            except Exception as _re:
                logger.warning("Basic mode page render failed: %s", _re)

        if effective_mode == "benchmark" and bench_form:
            bench_bytes = _read_pdf_bytes(project_id, bench_form.stored_filename)
            benchmark_doc = extract_all(bench_bytes)

            try:
                from engine.visual_diff import VisualDiff
                vd = VisualDiff(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))
                visual = vd.compare_pdfs_detailed(
                    original_pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    expected_pdf_path=_pdf_abs_path(project_id, bench_form.stored_filename),
                    result_id=f"run{run_id}_rr{rr_id}",
                    dpi=150,
                ) or []

                # Fallback: if detailed comparison produced nothing (e.g. large PDFs
                # caused a timeout), render the main form's pages individually so the
                # decision table at least has snapshot links for mismatch rows.
                if not visual:
                    logger.warning(
                        "compare_pdfs_detailed returned no rows for run %d — "
                        "falling back to single-panel snapshots.",
                        run_id,
                    )
                    try:
                        visual = vd.render_pages(
                            pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                            result_id=f"run{run_id}_rr{rr_id}_fb",
                        ) or []
                    except Exception as _fbe:
                        logger.warning("Fallback render_pages failed: %s", _fbe)

                # Document-level comparison: metadata and form field structure
                metadata_diff = {}
                field_diff = {}
                try:
                    metadata_diff = vd.compare_documents_metadata(
                        expected_path=_pdf_abs_path(project_id, bench_form.stored_filename),
                        actual_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    )
                    field_diff = vd.compare_form_field_structure(
                        expected_path=_pdf_abs_path(project_id, bench_form.stored_filename),
                        actual_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    )
                except Exception as _de:
                    logger.warning("Document-level comparison failed: %s", _de)

            except Exception as e:
                visual = []
                metadata_diff = {}
                field_diff = {}
                current_doc.setdefault("meta", {})
                current_doc["meta"]["visual_diff_error"] = str(e)
                logger.error("Benchmark visual diff failed: %s", e, exc_info=True)

            current_doc["visual_diffs"] = visual
            if benchmark_doc is not None:
                benchmark_doc["visual_diffs"] = visual

        extra_context = (
            {"metadata_diff": metadata_diff, "field_diff": field_diff}
            if effective_mode == "benchmark"
            else {}
        )

        # Render page images for multimodal LLM analysis in benchmark mode.
        # Vision lets the LLM detect watermarks, table structure, section placement,
        # and other layout details that text extraction alone misses.
        baseline_images = []
        current_images_llm = []
        if effective_mode == "benchmark" and bench_form:
            try:
                from engine.visual_diff import VisualDiff as _VD2
                _vd_img = _VD2(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))
                baseline_images = _vd_img.render_pages_for_llm(
                    _pdf_abs_path(project_id, bench_form.stored_filename)
                )
                current_images_llm = _vd_img.render_pages_for_llm(
                    _pdf_abs_path(project_id, main_form.stored_filename)
                )
            except Exception as _img_err:
                logger.warning("Page image render for LLM failed: %s", _img_err)

        messages = build_prompt(
            mode=effective_mode,
            current_doc=current_doc,
            benchmark_doc=benchmark_doc,
            base_prompt=rules_text,
            extra_context=extra_context,
            baseline_images=baseline_images or None,
            current_images=current_images_llm or None,
        )

        llm_out = run_validation(messages)
        result_json = _ensure_schema_defaults(llm_out, effective_mode)

        result_json["visual_validation"] = visual
        if effective_mode == "benchmark":
            result_json["_field_diff"] = field_diff
            vde = (current_doc.get("meta") or {}).get("visual_diff_error")
            if vde:
                result_json["visual_diff_error"] = vde

        result_json = _reconcile_visual_findings(result_json)

        # Mode-specific bucket enforcement: strip findings the LLM adds outside
        # the intended scope regardless of prompt instructions.
        if effective_mode == "specific":
            # Specific mode: only assertion-driven buckets are valid.
            # Keep: value_mismatches, missing_content, compliance_issues.
            # Clear everything else — LLM routinely adds unsolicited findings
            # to format_issues / extra_content / etc. even when assertions pass,
            # causing false REVIEW/FAIL verdicts.
            for _oos in (
                "spelling_errors", "format_issues", "extra_content",
                "layout_anomalies", "typography_issues",
                "accessibility_issues", "visual_mismatches", "structural_changes",
            ):
                result_json[_oos] = []

        if effective_mode == "basic":
            # Basic mode: spelling errors on insurance/legal forms are always
            # OCR or rendering noise — never genuine defects. Strip them so they
            # don't inflate the error count or mislead the tester.
            result_json["spelling_errors"] = []

        result_json = _refresh_summary_fields(result_json)

        # Suppress findings that match known false-positive patterns learned from
        # previous reviewer decisions, so the same observation doesn't recur.
        try:
            from app.services.auto_learning import suppress_false_positives
            form_id = main_form.id if main_form else None
            result_json = suppress_false_positives(result_json, project_id=project_id, form_id=form_id)
            # Benchmark reconciler: re-add visual_mismatches for pages with significant
            # pixel diff that the LLM missed entirely (as opposed to FP-suppressed).
            if effective_mode == "benchmark":
                result_json = _reconcile_benchmark_visual(result_json)
            result_json = _refresh_summary_fields(result_json)
        except Exception as _sfe:
            logger.warning("False-positive suppression failed: %s", _sfe)

        # Annotate snapshots with problem-area boxes for basic/specific modes.
        # Done after reconcile so all findings (including LLM + reconciler) are present.
        if effective_mode in ("basic", "specific") and visual:
            try:
                from engine.visual_diff import VisualDiff
                vd_ann = VisualDiff(output_dir=os.path.join(current_app.instance_path, "visual_diffs"))
                vd_ann.annotate_snapshots_with_findings(
                    pdf_path=_pdf_abs_path(project_id, main_form.stored_filename),
                    result_json=result_json,
                    visual_entries=visual,
                )
            except Exception as _ae:
                logger.warning("Snapshot annotation failed: %s", _ae)

        overall = (result_json.get("overall_summary") or "").strip()
        if result_json.get("error"):
            summary_text = f"Overall Assessment: FAIL\n{result_json.get('error')}"
        elif overall:
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