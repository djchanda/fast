"""
FAST batch runner — orchestrates extraction → LLM → visual diff → HTML report.

Reuses the engine modules unchanged; all Flask/DB dependencies are absent.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── add repo root to path so engine/ and app/ are importable ────────────────
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent   # batch_process/batch/ → batch_process/ → repo root
sys.path.insert(0, str(_REPO_ROOT))

from engine.extractor import extract_all           # noqa: E402
from engine.llm_client import run_validation        # noqa: E402
from engine.prompt_builder import build_prompt      # noqa: E402

from batch.manifest_loader import BatchConfig, TestEntry  # noqa: E402
from batch.batch_reporter import write_batch_report        # noqa: E402
from batch.console import print_running, print_result, print_error  # noqa: E402

VISUAL_REVIEW_SIMILARITY_THRESHOLD     = 0.9985
VISUAL_SIGNATURE_SIMILARITY_THRESHOLD  = 0.9990


# ── helper functions (adapted from app/services/runner.py — no Flask deps) ──

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
        "compliance_issues": [],
        "visual_mismatches": [],
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
        "spelling_errors", "format_issues", "value_mismatches",
        "missing_content", "extra_content", "layout_anomalies",
        "compliance_issues", "visual_mismatches", "pages_impacted",
        "top_findings", "visual_validation",
    ]:
        if base.get(k) is None:
            base[k] = []
        if k in base and not isinstance(base[k], list):
            base[k] = [base[k]]
    if not isinstance(base.get("summary_counts"), dict):
        base["summary_counts"] = {}
    return base


def _safe_similarity(v: dict) -> float:
    try:
        return float(v.get("similarity"))
    except Exception:
        return 1.0


def _contains_signature_issue(page_items: list) -> bool:
    keywords = ("signature", "president", "secretary", "approval", "signed")
    blob = " ".join(
        str(x) for item in page_items for x in item.values()
        if not isinstance(x, (dict, list))
    ).lower()
    return any(k in blob for k in keywords)


def _page_has_real_business_issue(page_items: list) -> bool:
    strong = {"value_mismatches", "missing_content", "extra_content", "compliance_issues"}
    return any(isinstance(i, dict) and i.get("_bucket") in strong for i in page_items)


def _index_existing_items_by_page(result_json: dict) -> Dict[int, list]:
    by_page: Dict[int, list] = {}
    for bucket in [
        "value_mismatches", "missing_content", "extra_content", "layout_anomalies",
        "visual_mismatches", "format_issues", "spelling_errors", "compliance_issues",
    ]:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                p = it.get("page")
                try:
                    p = int(str(p).strip()) if p not in (None, "") else None
                except Exception:
                    p = None
                if p is not None:
                    by_page.setdefault(p, []).append(dict(it) | {"_bucket": bucket})
    return by_page


def _reconcile_visual_findings(result_json: dict) -> dict:
    visual = result_json.get("visual_validation") or []
    if not isinstance(visual, list):
        visual = []

    existing = _index_existing_items_by_page(result_json)
    result_json.setdefault("visual_mismatches", [])
    result_json.setdefault("missing_content", [])

    for v in visual:
        if not isinstance(v, dict):
            continue
        op = str(v.get("alignment_op") or "matched").lower()

        if op == "deleted":
            pg = v.get("expected_page_num") or v.get("page")
            result_json["missing_content"].append({
                "page": pg, "severity": "critical",
                "field_name": f"page_{pg}", "category": "Removed page",
                "description": f"Page {pg} of the expected PDF is absent in the actual PDF.",
                "evidence": v.get("note") or "Page missing from actual PDF.",
            })
            continue

        if op == "inserted":
            pg = v.get("actual_page_num") or v.get("page")
            result_json["extra_content"].append({
                "page": pg, "severity": "critical",
                "field_name": f"page_{pg}", "category": "Inserted page",
                "description": f"Page {pg} of the actual PDF has no counterpart in the expected PDF.",
                "evidence": v.get("note") or "Extra page found in actual PDF.",
            })
            continue

        try:
            p = int(str(v.get("page")).strip()) if v.get("page") not in (None, "") else None
        except Exception:
            p = None
        if p is None:
            continue

        page_items   = existing.get(p, [])
        already_sig  = _contains_signature_issue(page_items)
        has_biz      = _page_has_real_business_issue(page_items)
        sim          = _safe_similarity(v)
        sig_conf     = str(v.get("signature_confidence") or "none").lower()

        if (
            v.get("signature_candidate")
            and sig_conf in {"high", "medium"}
            and sim <= VISUAL_SIGNATURE_SIMILARITY_THRESHOLD
            and not already_sig
        ):
            label  = v.get("signature_label") or "signature"
            reason = v.get("signature_reason") or f"Visual diff near '{label}' in signature zone."
            result_json["missing_content"].append({
                "page": p,
                "severity": "high" if sig_conf == "high" else "medium",
                "field_name": f"{str(label).lower()}_signature",
                "category": "Signature / approval block",
                "description": f"Possible missing or changed signature near {label}.",
                "evidence": reason,
            })
            existing.setdefault(p, []).append({"page": p, "description": reason, "_bucket": "missing_content"})
            continue

        if (
            (v.get("major") or v.get("warn"))
            and sim <= VISUAL_REVIEW_SIMILARITY_THRESHOLD
            and not page_items
            and not has_biz
        ):
            result_json["visual_mismatches"].append({
                "page": p,
                "severity": "medium" if v.get("warn") else "high",
                "category": "Visual difference",
                "description": v.get("note") or "Visual difference detected.",
                "evidence": f"similarity={v.get('similarity')}, diff_pixels_pct={v.get('diff_pixels_pct')}",
            })

        if v.get("signature_candidate") and sig_conf in {"high", "medium"} and not already_sig:
            label  = v.get("signature_label") or "signature"
            reason = v.get("signature_reason") or f"Visual diff near '{label}' in signature zone."
            result_json["missing_content"].append({
                "page": p, "severity": "high",
                "field_name": f"{str(label).lower()}_signature",
                "category": "Signature / approval block",
                "description": f"Signature missing or changed near {label}.",
                "evidence": reason,
            })

    return result_json


def _refresh_summary_fields(result_json: dict) -> dict:
    buckets = [
        "spelling_errors", "format_issues", "value_mismatches",
        "missing_content", "extra_content", "layout_anomalies", "visual_mismatches",
    ]
    counts = {k: len(result_json.get(k, []) or []) for k in buckets}
    counts["total"] = sum(counts.values())
    result_json["summary_counts"] = counts

    pages: set = set()
    for bucket in buckets + ["compliance_issues"]:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                p = it.get("page")
                try:
                    if p not in (None, ""):
                        pages.add(int(str(p).strip()))
                except Exception:
                    pass
    result_json["pages_impacted"] = sorted(pages)

    merged: list = []
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for bucket in buckets:
        for it in result_json.get(bucket, []) or []:
            if isinstance(it, dict):
                merged.append({
                    "severity": str(it.get("severity") or "medium").lower(),
                    "page":     it.get("page", ""),
                    "category": it.get("category") or bucket.replace("_", " ").title(),
                    "short":    it.get("description") or it.get("field_name") or bucket,
                })
    merged.sort(key=lambda x: sev_order.get(x["severity"], 9))
    result_json["top_findings"] = merged[:5]

    if not result_json.get("overall_summary"):
        total = counts["total"]
        pg_ct = len(result_json["pages_impacted"])
        top   = "; ".join(x["short"] for x in result_json["top_findings"][:3]) or "none"
        result_json["overall_summary"] = (
            f"Found {total} issue(s) across {pg_ct} page(s). Top findings: {top}."
        )
    return result_json


def _derive_metrics(result_json: dict) -> tuple[int, int, int]:
    errors = (
        _count_list(result_json.get("spelling_errors"))
        + _count_list(result_json.get("format_issues"))
        + _count_list(result_json.get("value_mismatches"))
        + _count_list(result_json.get("missing_content"))
        + _count_list(result_json.get("compliance_issues"))
        + _count_list(result_json.get("visual_mismatches"))
    )
    warnings = (
        _count_list(result_json.get("extra_content"))
        + _count_list(result_json.get("layout_anomalies"))
    )
    visual = result_json.get("visual_validation") or []
    if isinstance(visual, list):
        promoted = {
            int(str(it.get("page")).strip())
            for it in (result_json.get("visual_mismatches") or []) + (result_json.get("missing_content") or [])
            if isinstance(it, dict) and it.get("page") not in (None, "")
        }
        warned = {
            int(str(v.get("page")).strip())
            for v in visual
            if isinstance(v, dict)
            and v.get("warn")
            and v.get("page") not in (None, "")
            and _safe_similarity(v) <= VISUAL_REVIEW_SIMILARITY_THRESHOLD
        }
        warnings += len([p for p in warned if p not in promoted])

    return errors, warnings, (1 if errors == 0 else 0)


def _status_from_result(result_json: dict) -> str:
    """Derive PASS / REVIEW / FAIL / CRITICAL from the result JSON."""
    from batch.batch_reporter import _page_decisions
    decisions  = _page_decisions(result_json)
    mismatches = [d for d in decisions if d["status"] == "mismatch"]
    reviews    = [d for d in decisions if d["status"] == "review"]

    # Check if any finding is CRITICAL severity
    all_findings = (
        (result_json.get("value_mismatches") or [])
        + (result_json.get("missing_content") or [])
        + (result_json.get("compliance_issues") or [])
    )
    has_critical = any(
        isinstance(f, dict) and str(f.get("severity") or "").lower() == "critical"
        for f in all_findings
    )

    if has_critical:
        return "CRITICAL"
    if mismatches:
        return "FAIL"
    if reviews:
        return "REVIEW"
    return "PASS"


# ── public API ────────────────────────────────────────────────────────────────

def run_one(
    entry: TestEntry,
    config: BatchConfig,
    run_index: int,
    snapshots_dir: str,
) -> dict:
    """
    Run a single test entry. Returns a result dict with keys:
      name, status, errors, warnings, passed, report_path, error_message
    """
    try:
        mode = (entry.mode or "basic").strip().lower()
        if mode == "specific" and not entry.prompt:
            mode = "basic"

        # ── Extract current PDF ───────────────────────────────────────────────
        with open(entry.current_pdf, "rb") as fh:
            current_bytes = fh.read()
        current_doc = extract_all(current_bytes)

        # ── Extract benchmark PDF & run visual diff ───────────────────────────
        benchmark_doc = None
        visual: list = []

        if mode == "benchmark" and entry.benchmark_pdf:
            with open(entry.benchmark_pdf, "rb") as fh:
                bench_bytes = fh.read()
            benchmark_doc = extract_all(bench_bytes)

            try:
                from engine.visual_diff import VisualDiff
                vd = VisualDiff(output_dir=snapshots_dir)
                visual = vd.compare_pdfs_detailed(
                    original_pdf_path=entry.current_pdf,
                    expected_pdf_path=entry.benchmark_pdf,
                    result_id=f"batch_{run_index}",
                ) or []
            except Exception as ve:
                current_doc.setdefault("meta", {})
                current_doc["meta"]["visual_diff_error"] = str(ve)

            current_doc["visual_diffs"]  = visual
            if benchmark_doc is not None:
                benchmark_doc["visual_diffs"] = visual

        # ── Build prompt & call LLM ───────────────────────────────────────────
        messages = build_prompt(
            mode=mode,
            current_doc=current_doc,
            benchmark_doc=benchmark_doc,
            base_prompt=entry.prompt,
        )
        llm_out = run_validation(messages, provider=config.llm_provider)

        # ── Post-process ──────────────────────────────────────────────────────
        result_json = _ensure_schema_defaults(llm_out, mode)
        result_json["visual_validation"] = visual
        result_json = _reconcile_visual_findings(result_json)
        result_json = _refresh_summary_fields(result_json)

        errors, warnings, passed = _derive_metrics(result_json)
        status = _status_from_result(result_json)

        # ── Write HTML report ─────────────────────────────────────────────────
        report_path = write_batch_report(
            output_dir=config.output_dir,
            test_name=entry.name,
            mode=mode,
            result_json=result_json,
            main_form_name=os.path.basename(entry.current_pdf),
            bench_form_name=os.path.basename(entry.benchmark_pdf) if entry.benchmark_pdf else "",
            project_name=config.project_name,
            environment=config.environment,
            account=config.account,
            run_index=run_index,
        )

        return {
            "name":          entry.name,
            "status":        status,
            "errors":        errors,
            "warnings":      warnings,
            "passed":        passed,
            "report_path":   report_path,
            "error_message": "",
        }

    except Exception as exc:
        return {
            "name":          entry.name,
            "status":        "ERROR",
            "errors":        1,
            "warnings":      0,
            "passed":        0,
            "report_path":   "",
            "error_message": str(exc),
        }


def run_all(config: BatchConfig) -> List[dict]:
    """Run all test entries in the manifest. Prints live progress."""
    snapshots_dir = os.path.join(config.output_dir, "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)

    results: List[dict] = []
    total = len(config.tests)

    for i, entry in enumerate(config.tests):
        print_running(entry.name, i + 1, total)
        t0 = time.time()
        result = run_one(entry, config, run_index=i, snapshots_dir=snapshots_dir)
        elapsed = time.time() - t0

        if result["status"] == "ERROR":
            print_error(result["name"], result["error_message"])
        else:
            print_result(
                result["name"],
                result["status"],
                result["errors"],
                result["warnings"],
                result["report_path"],
            )
        print(f"           {elapsed:.1f}s", flush=True)
        results.append(result)

    return results
