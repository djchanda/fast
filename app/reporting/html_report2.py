# app/reporting/html_report.py
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, url_for


# ============================================================
# Paths
# ============================================================

def _reports_dir() -> Path:
    d = Path(current_app.instance_path) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Small helpers
# ============================================================

def _safe_slug(s: str) -> str:
    s = (s or "test").strip()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    return (s[:80] or "test")


def _esc(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _pick_first(d: dict, keys: list[str], default=""):
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def _to_int_page(v: Any) -> Optional[int]:
    if v in (None, "", "null"):
        return None
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _shorten(text: Any, n: int = 180) -> str:
    s = str(text or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _severity_rank(sev: str) -> int:
    sev = (sev or "").strip().lower()
    if sev == "critical":
        return 0
    if sev == "high":
        return 1
    if sev in ("medium", "warn", "warning"):
        return 2
    if sev in ("low", "info"):
        return 3
    return 4


def _chip(text: str, kind: str) -> str:
    cls = {
        "ok": "chip chip-ok",
        "warn": "chip chip-warn",
        "bad": "chip chip-bad",
        "info": "chip chip-info",
        "neutral": "chip chip-neutral",
    }.get(kind, "chip chip-info")
    return f"<span class='{cls}'>{_esc(text)}</span>"


def _severity_chip(sev: str) -> str:
    sev_l = (sev or "").strip().lower()
    if sev_l in ("critical", "high"):
        return _chip(sev_l.upper(), "bad")
    if sev_l in ("medium", "warn", "warning"):
        return _chip(sev_l.upper(), "warn")
    if sev_l in ("low", "info"):
        return _chip(sev_l.upper(), "info")
    return _chip((sev or "INFO").upper(), "neutral")


def _status_chip(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "match":
        return _chip("MATCH", "ok")
    if s == "review":
        return _chip("REVIEW", "warn")
    if s == "mismatch":
        return _chip("MISMATCH", "bad")
    return _chip(status or "UNKNOWN", "neutral")


def _extract_page(it: dict) -> Optional[int]:
    if not isinstance(it, dict):
        return None

    direct = _pick_first(
        it,
        ["page", "page_num", "page_no", "page_number", "pageno", "pg", "pageIndex"],
        default=None,
    )
    if direct not in (None, ""):
        return _to_int_page(direct)

    loc = it.get("location") or it.get("loc") or {}
    if isinstance(loc, dict):
        nested = _pick_first(
            loc,
            ["page", "page_num", "page_no", "page_number", "pageno", "pg"],
            default=None,
        )
        if nested not in (None, ""):
            return _to_int_page(nested)

    src = it.get("source") or {}
    if isinstance(src, dict):
        nested = _pick_first(src, ["page", "page_num", "page_number"], default=None)
        if nested not in (None, ""):
            return _to_int_page(nested)

    return None


def _extract_field(it: dict) -> str:
    if not isinstance(it, dict):
        return ""
    v = _pick_first(
        it,
        [
            "field",
            "field_name",
            "fieldName",
            "name",
            "label",
            "key",
            "attribute",
            "attribute_name",
            "data_field",
            "dataField",
        ],
        default="",
    )
    if v not in ("", None):
        return str(v)

    f = it.get("field")
    if isinstance(f, dict):
        return str(_pick_first(f, ["name", "label", "key", "field_name"], default=""))

    return ""


def _snapshot_link(project_id: int, snapshot_path: str) -> str:
    if not snapshot_path:
        return "—"
    img_name = str(snapshot_path).split("/")[-1]
    href = url_for(
        "web.serve_visual_diff_file",
        project_id=project_id,
        filename=img_name,
        _external=True,
    )
    return f"<a href='{_esc(href)}' target='_blank' rel='noopener'>View</a>"


# ============================================================
# Data normalization
# ============================================================

def _normalize_result_json(result_json: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(result_json or {})
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
        out[k] = _as_list(out.get(k))

    out["overall_summary"] = str(out.get("overall_summary") or "").strip()
    out["error"] = str(out.get("error") or "").strip()
    return out


def _llm_failed(result_json: Dict[str, Any]) -> bool:
    err = str(result_json.get("error") or "").lower()
    return bool(err and ("gemini" in err or "llm" in err or "request failed" in err or "failed" in err))


# ============================================================
# Issue extraction
# ============================================================

def _collect_llm_issues(result_json: Dict[str, Any]) -> List[dict]:
    bucket_meta = [
        ("spelling_errors", "Spelling", "low"),
        ("format_issues", "Format", "low"),
        ("value_mismatches", "Value", "critical"),
        ("missing_content", "Missing content", "high"),
        ("extra_content", "Extra content", "medium"),
        ("layout_anomalies", "Layout", "medium"),
        ("compliance_issues", "Compliance", "high"),
    ]

    items: List[dict] = []

    for bucket, category, default_sev in bucket_meta:
        for it in _as_list(result_json.get(bucket)):
            if not isinstance(it, dict):
                continue

            page = _extract_page(it)
            severity = str(it.get("severity") or default_sev).lower()
            field = _extract_field(it)
            description = _pick_first(
                it,
                ["description", "details", "reason", "note", "evidence"],
                default="",
            )
            expected = _pick_first(it, ["expected", "expected_value", "expectedValue"], default="")
            actual = _pick_first(it, ["actual", "actual_value", "actualValue"], default="")

            if not description:
                if bucket == "value_mismatches":
                    description = f"{field or 'Field'} changed from {expected or '?'} to {actual or '?'}"
                elif bucket == "spelling_errors":
                    description = f"Spelling issue: {_pick_first(it, ['text'], default='')}"
                else:
                    description = category

            items.append(
                {
                    "page": page,
                    "bucket": bucket,
                    "category": category,
                    "severity": severity,
                    "field": field,
                    "description": str(description).strip(),
                    "expected": str(expected).strip(),
                    "actual": str(actual).strip(),
                    "raw": it,
                }
            )

    return items


def _group_llm_issues_by_page(items: List[dict]) -> Dict[int, List[dict]]:
    grouped: Dict[int, List[dict]] = defaultdict(list)
    for it in items:
        p = it.get("page")
        if p is not None:
            grouped[int(p)].append(it)
    return grouped


# ============================================================
# Visual inference
# ============================================================

_SIGNATURE_HINTS = [
    "signature",
    "president",
    "secretary",
    "authorized",
    "signed",
    "approval",
]

_FOOTER_HINTS = [
    "footer",
    "header",
    "pagination",
    "page",
    "page numbering",
]

_LAYOUT_HINTS = [
    "layout",
    "table",
    "misaligned",
    "alignment",
    "shift",
    "moved",
]


def _looks_like_signature_issue(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _SIGNATURE_HINTS)


def _looks_like_footer_noise(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _FOOTER_HINTS)


def _looks_like_layout_issue(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _LAYOUT_HINTS)


def _infer_visual_row(
    v: dict,
    page_llm_items: List[dict],
) -> Tuple[str, str, str]:
    """
    Returns: (category, severity, summary)
    """
    note = str(v.get("note") or "").strip()
    note_l = note.lower()

    # Let strong LLM page issues dominate
    for item in sorted(page_llm_items, key=lambda x: _severity_rank(x.get("severity", ""))):
        desc = f"{item.get('field', '')} {item.get('description', '')}".lower()
        if item["bucket"] == "value_mismatches":
            return (
                "Value difference",
                "critical",
                item["description"] or "Value changed",
            )
        if item["bucket"] == "missing_content" and _looks_like_signature_issue(desc):
            return (
                "Signature / approval block",
                "high",
                item["description"] or "Signature block changed",
            )
        if item["bucket"] == "missing_content":
            return (
                "Missing content",
                "high",
                item["description"] or "Content missing",
            )
        if item["bucket"] == "extra_content":
            return (
                "Extra content",
                "medium",
                item["description"] or "Unexpected content present",
            )
        if item["bucket"] == "layout_anomalies":
            return (
                "Layout difference",
                "medium",
                item["description"] or "Layout changed",
            )

    # If no LLM item, infer from note
    if _looks_like_signature_issue(note_l):
        return ("Signature / approval block", "high", note or "Possible signature-related visual change")
    if _looks_like_layout_issue(note_l):
        return ("Layout difference", "medium", note or "Layout changed")
    if _looks_like_footer_noise(note_l):
        return ("Header / footer / pagination", "low", note or "Header/footer difference")

    # Infer from visual flags
    if v.get("major"):
        return ("Visual difference", "high", note or "Significant visual change detected")
    if v.get("warn"):
        return ("Visual difference", "medium", note or "Visual change detected")

    return ("No material difference", "low", note or "No material visual differences detected")


# ============================================================
# Page decision engine
# ============================================================

def _build_page_decisions(
    result_json: Dict[str, Any]
) -> Tuple[List[dict], List[int], Dict[int, List[dict]]]:
    """
    Returns:
      - page_decisions: one row per page from deterministic merge
      - all_pages_seen
      - llm issues by page
    """
    llm_items = _collect_llm_issues(result_json)
    llm_by_page = _group_llm_issues_by_page(llm_items)
    visual_rows = _as_list(result_json.get("visual_validation"))

    pages_seen = set()
    for p in llm_by_page.keys():
        pages_seen.add(int(p))
    for v in visual_rows:
        if isinstance(v, dict):
            p = _to_int_page(v.get("page"))
            if p is not None:
                pages_seen.add(p)

    page_decisions: List[dict] = []

    for page in sorted(pages_seen):
        llm_page_items = llm_by_page.get(page, [])
        visuals = [v for v in visual_rows if isinstance(v, dict) and _to_int_page(v.get("page")) == page]

        status = "match"
        category = "No difference"
        severity = "low"
        summary = "All matching."
        evidence_parts: List[str] = []
        snapshot = ""
        similarity = ""
        source = "Deterministic"

        # LLM issues first
        if llm_page_items:
            top = sorted(llm_page_items, key=lambda x: _severity_rank(x.get("severity", "")))[0]
            category = top["category"]
            severity = top["severity"]
            summary = top["description"] or category
            source = "LLM + deterministic"

            if top["bucket"] in ("value_mismatches", "missing_content", "extra_content", "compliance_issues"):
                status = "mismatch"
            elif top["bucket"] in ("layout_anomalies", "format_issues", "spelling_errors"):
                status = "review"

            for item in llm_page_items[:5]:
                chunk = item["description"]
                if item.get("field"):
                    chunk = f"{item['field']}: {chunk}"
                evidence_parts.append(chunk)

        # Visual evidence can upgrade or add decision
        if visuals:
            visual = sorted(
                visuals,
                key=lambda x: (0 if x.get("major") else 1 if x.get("warn") else 2, x.get("page", 9999))
            )[0]

            v_category, v_sev, v_summary = _infer_visual_row(visual, llm_page_items)
            similarity_val = visual.get("similarity")
            if isinstance(similarity_val, (int, float)):
                similarity = f"{float(similarity_val) * 100:.3f}%"
            else:
                similarity = str(similarity_val or "")

            snapshot = str(visual.get("snapshot_path") or "")

            # Strong visual logic
            if visual.get("major"):
                if status != "mismatch":
                    status = "review"
                if _severity_rank(v_sev) < _severity_rank(severity):
                    severity = v_sev
                    category = v_category
                    summary = v_summary

            elif visual.get("warn"):
                # Signature-like page + warn => mismatch candidate
                combined_text = " ".join(
                    [summary, v_summary, visual.get("note", "")]
                    + [x["description"] for x in llm_page_items]
                )
                if _looks_like_signature_issue(combined_text):
                    status = "mismatch"
                    severity = "high"
                    category = "Signature / approval block"
                    summary = v_summary if v_summary else "Possible missing or changed signature block"
                elif status == "match":
                    status = "review"
                    if _severity_rank(v_sev) < _severity_rank(severity):
                        severity = v_sev
                        category = v_category
                        summary = v_summary

            # add note as evidence if useful
            note = str(visual.get("note") or "").strip()
            if note and note.lower() != "no material visual differences detected":
                evidence_parts.append(note)

        # If no llm + no material visual => match
        if not llm_page_items and visuals:
            visual = visuals[0]
            if not visual.get("major") and not visual.get("warn"):
                status = "match"
                category = "No difference"
                severity = "low"
                summary = "All matching."
                evidence_parts = []

        # Cleanup summary for clean pages
        if status == "match":
            category = "No difference"
            severity = "low"
            summary = "All matching."
            evidence_parts = []

        page_decisions.append(
            {
                "page": page,
                "status": status,
                "category": category,
                "severity": severity,
                "summary": summary,
                "evidence": " | ".join(dict.fromkeys([e for e in evidence_parts if e]))[:700],
                "snapshot_path": snapshot,
                "similarity": similarity or "—",
                "source": source,
            }
        )

    return page_decisions, sorted(pages_seen), llm_by_page


def _compress_matching_pages(page_decisions: List[dict]) -> str:
    matches = sorted([d["page"] for d in page_decisions if d["status"] == "match"])
    if not matches:
        return "None"

    ranges = []
    start = prev = matches[0]
    for p in matches[1:]:
        if p == prev + 1:
            prev = p
        else:
            ranges.append((start, prev))
            start = prev = p
    ranges.append((start, prev))

    parts = []
    for a, b in ranges:
        if a == b:
            parts.append(str(a))
        else:
            parts.append(f"{a}-{b}")
    return ", ".join(parts)


# ============================================================
# HTML table helpers
# ============================================================

def _render_simple_table(headers: List[str], rows: List[List[str]], min_width: int = 960) -> str:
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = ""
    if rows:
        for row in rows:
            body += "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
    else:
        body = f"<tr><td colspan='{len(headers)}' class='muted'>None</td></tr>"

    return f"""
      <div class="table-wrap">
        <table class="fast-table" style="min-width:{min_width}px;">
          <thead><tr>{thead}</tr></thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    """


def _render_detail_table(items: List[dict], title: str, columns: List[Tuple[str, str]]) -> str:
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = []
        for key, _hdr in columns:
            if key == "page":
                row.append(_esc(str(_extract_page(it) or "")))
            elif key == "severity":
                row.append(_severity_chip(str(it.get("severity") or "info")))
            elif key == "field":
                row.append(_esc(_extract_field(it)))
            else:
                val = it.get(key, "")
                if key == "description" and val in ("", None):
                    val = _pick_first(it, ["details", "reason", "note", "evidence"], default="")
                if key == "expected" and val in ("", None):
                    val = _pick_first(it, ["expected_value", "expectedValue"], default="")
                if key == "actual" and val in ("", None):
                    val = _pick_first(it, ["actual_value", "actualValue"], default="")
                row.append(_esc(_shorten(val, 220)))
        rows.append(row)

    return f"""
      <div class="subcard">
        <div class="subcard-title">{_esc(title)}</div>
        {_render_simple_table([h for _, h in columns], rows)}
      </div>
    """


# ============================================================
# Main report writer
# ============================================================

def write_cli_style_report(
    *,
    project_id: int,
    run_id: int,
    rr_id: int,
    tc: Any,
    result_json: Dict[str, Any],
    llm_summary: str = "",
    main_form: Optional[Any] = None,
    bench_form: Optional[Any] = None,
) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tc_name = _safe_slug(getattr(tc, "name", "test"))
    filename = f"{tc_name}_{ts}.html"
    out_path = _reports_dir() / filename

    result_json = _normalize_result_json(result_json)
    if llm_summary and not result_json.get("overall_summary"):
        result_json["overall_summary"] = str(llm_summary).strip()

    mode = (result_json.get("mode") or getattr(tc, "mode", "") or "").strip().lower()
    llm_failed = _llm_failed(result_json)

    spelling = _as_list(result_json.get("spelling_errors"))
    fmt_issues = _as_list(result_json.get("format_issues"))
    mismatches = _as_list(result_json.get("value_mismatches"))
    missing = _as_list(result_json.get("missing_content"))
    extra = _as_list(result_json.get("extra_content"))
    layout = _as_list(result_json.get("layout_anomalies"))
    compliance = _as_list(result_json.get("compliance_issues"))
    visual_validation = _as_list(result_json.get("visual_validation"))

    page_decisions, all_pages_seen, _llm_by_page = _build_page_decisions(result_json)

    mismatch_pages = [d for d in page_decisions if d["status"] == "mismatch"]
    review_pages = [d for d in page_decisions if d["status"] == "review"]
    match_pages = [d for d in page_decisions if d["status"] == "match"]

    matched_ranges = _compress_matching_pages(page_decisions)

    # Summary text
    if llm_failed:
        summary_text = (
            "LLM classification failed. Showing deterministic visual/text comparison only. "
            "Use the page-by-page decision table below as the source of truth for this run."
        )
    else:
        summary_text = result_json.get("overall_summary") or ""
        if not summary_text:
            summary_text = (
                f"Found {len(mismatch_pages)} mismatch page(s) and {len(review_pages)} review page(s). "
                f"Matched pages: {matched_ranges if matched_ranges != 'None' else 'none'}."
            )

    # Verdict
    if llm_failed and not page_decisions:
        verdict = _chip("ERROR", "bad")
    elif mismatch_pages:
        verdict = _chip("FAIL", "bad")
    elif review_pages:
        verdict = _chip("REVIEW", "warn")
    else:
        verdict = _chip("PASS", "ok")

    # Form links
    main_pdf_link = ""
    bench_pdf_link = ""
    if main_form and getattr(main_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=main_form.id, _external=True)
        main_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"
    if bench_form and getattr(bench_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=bench_form.id, _external=True)
        bench_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"

    # Top page-by-page table: only mismatch + review
    decision_rows: List[List[str]] = []
    for d in [*mismatch_pages, *review_pages]:
        snap = "—"
        if d.get("snapshot_path"):
            snap = _snapshot_link(project_id, d["snapshot_path"])

        decision_rows.append(
            [
                _esc(str(d["page"])),
                _status_chip(d["status"]),
                _severity_chip(d["severity"]),
                _esc(d["category"]),
                _esc(_shorten(d["summary"], 180)),
                _esc(_shorten(d["evidence"], 260)),
                _esc(d["similarity"]),
                snap,
            ]
        )

    # Detailed raw findings only if LLM succeeded
    show_llm_detail = not llm_failed

    # Compact visual table: only warn/major or pages already flagged
    flagged_pages = {d["page"] for d in mismatch_pages + review_pages}
    visual_rows: List[List[str]] = []
    for v in visual_validation:
        if not isinstance(v, dict):
            continue
        p = _to_int_page(v.get("page"))
        if p is None:
            continue
        if not v.get("major") and not v.get("warn") and p not in flagged_pages:
            continue

        sim = v.get("similarity")
        sim_text = f"{float(sim) * 100:.3f}%" if isinstance(sim, (int, float)) else _esc(str(sim or "—"))
        severity = "high" if v.get("major") else "medium" if v.get("warn") else "low"
        note = str(v.get("note") or "").strip()
        snap = _snapshot_link(project_id, str(v.get("snapshot_path") or ""))

        visual_rows.append(
            [
                _esc(str(p)),
                _severity_chip(severity),
                _esc(_shorten(note or "Visual comparison result", 220)),
                _esc(sim_text),
                snap,
            ]
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FAST Report - {_esc(tc_name)}</title>
  <style>
    :root{{
      --bg:#f6f8fb;
      --card:#ffffff;
      --text:#111827;
      --muted:#6b7280;
      --border:#e5e7eb;
      --shadow:0 2px 10px rgba(17,24,39,.05);

      --ok-bg:#ecfdf3; --ok-b:#b7f0c8; --ok-t:#0f7a3a;
      --warn-bg:#fff7ed; --warn-b:#fed7aa; --warn-t:#9a3412;
      --bad-bg:#fff1f2; --bad-b:#fecdd3; --bad-t:#9f1239;
      --info-bg:#eff6ff; --info-b:#bfdbfe; --info-t:#1d4ed8;
      --neutral-bg:#f3f4f6; --neutral-b:#d1d5db; --neutral-t:#374151;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }}

    .wrap {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 18px;
    }}

    .topbar, .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }}

    .topbar {{
      padding: 16px 18px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }}

    .card {{
      padding: 16px;
      margin-top: 14px;
    }}

    h1 {{
      margin: 0 0 6px 0;
      font-size: 18px;
    }}

    .muted {{
      color: var(--muted);
      font-size: 13px;
    }}

    .chip {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      letter-spacing: .2px;
    }}

    .chip-ok {{ background: var(--ok-bg); border-color: var(--ok-b); color: var(--ok-t); }}
    .chip-warn {{ background: var(--warn-bg); border-color: var(--warn-b); color: var(--warn-t); }}
    .chip-bad {{ background: var(--bad-bg); border-color: var(--bad-b); color: var(--bad-t); }}
    .chip-info {{ background: var(--info-bg); border-color: var(--info-b); color: var(--info-t); }}
    .chip-neutral {{ background: var(--neutral-bg); border-color: var(--neutral-b); color: var(--neutral-t); }}

    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px 20px;
    }}

    .kv {{
      display: flex;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px dashed #eef0f3;
    }}

    .kv:last-child {{ border-bottom: none; }}

    .kv-k {{
      width: 180px;
      color: var(--muted);
      font-size: 13px;
    }}

    .kv-v {{
      flex: 1;
      font-size: 13px;
    }}

    .section-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin: 0 0 10px 0;
      font-size: 15px;
      font-weight: 700;
    }}

    .subcard {{
      margin-top: 12px;
      padding-top: 8px;
    }}

    .subcard-title {{
      font-weight: 700;
      margin-bottom: 8px;
    }}

    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #fff;
      margin-top: 8px;
    }}

    .fast-table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 1000px;
    }}

    .fast-table th,
    .fast-table td {{
      padding: 10px 10px;
      border-bottom: 1px solid #eef0f3;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
      white-space: normal;
    }}

    .fast-table th {{
      background: #f9fafb;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .4px;
      color: #374151;
      position: sticky;
      top: 0;
      z-index: 2;
    }}

    .fast-table tbody tr:nth-child(even) {{
      background: #fbfcfe;
    }}

    .fast-table tbody tr:hover {{
      background: #f2f6fb;
    }}

    a {{
      color: #2563eb;
      text-decoration: none;
    }}

    a:hover {{
      text-decoration: underline;
    }}

    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}

    .banner {{
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid #fed7aa;
      background: #fff7ed;
      color: #9a3412;
      font-size: 13px;
    }}

    @media (max-width: 920px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .kv-k {{ width: 135px; }}
      .fast-table {{ min-width: 760px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">

    <div class="topbar">
      <div>
        <h1>FAST Validation Report</h1>
        <div class="muted">Test: <b>{_esc(getattr(tc, "name", ""))}</b> &nbsp;|&nbsp; Mode: <b>{_esc(mode)}</b></div>
        <div class="muted">Run #{run_id} / Result #{rr_id}</div>
      </div>
      <div>{verdict}</div>
    </div>

    <div class="card">
      <div class="grid">
        <div>
          <div class="kv">
            <div class="kv-k">Main Form</div>
            <div class="kv-v">{_esc(getattr(main_form, "original_filename", "") or getattr(main_form, "stored_filename", "") or "")}{" &nbsp;|&nbsp; " + main_pdf_link if main_pdf_link else ""}</div>
          </div>
          <div class="kv">
            <div class="kv-k">Benchmark Form</div>
            <div class="kv-v">{_esc(getattr(bench_form, "original_filename", "") or getattr(bench_form, "stored_filename", "") or "")}{" &nbsp;|&nbsp; " + bench_pdf_link if bench_pdf_link else ""}</div>
          </div>
        </div>
        <div>
          <div class="kv">
            <div class="kv-k">Generated</div>
            <div class="kv-v">{_esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</div>
          </div>
          <div class="kv">
            <div class="kv-k">Report File</div>
            <div class="kv-v mono">{_esc(filename)}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        <div>Decision Summary</div>
      </div>

      <div>{_esc(summary_text)}</div>

      {f"<div class='banner'><b>LLM classification unavailable.</b> { _esc(result_json.get('error')) }</div>" if llm_failed else ""}

      <div style="margin-top:12px; display:flex; flex-wrap:wrap; gap:8px;">
        {_chip(f"Mismatches: {len(mismatch_pages)}", "bad" if mismatch_pages else "ok")}
        {_chip(f"Review pages: {len(review_pages)}", "warn" if review_pages else "ok")}
        {_chip(f"Matched pages: {len(match_pages)}", "ok")}
        {_chip(f"Pages seen: {len(all_pages_seen)}", "info")}
      </div>

      <div style="margin-top:10px;">
        {_chip(f"Matched page ranges: {matched_ranges}", "info" if matched_ranges != "None" else "neutral")}
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        <div>Page-by-Page Decision</div>
        <div class="muted">Only mismatched or review pages are shown below. All other pages are grouped as matched.</div>
      </div>

      {_render_simple_table(
          ["Page", "Status", "Severity", "Category", "Decision", "Evidence", "Similarity", "Snapshot"],
          decision_rows,
          min_width=1180
      )}

      <div style="margin-top:10px;" class="muted">
        {("All pages matched." if not decision_rows else f"All other pages matched: {matched_ranges if matched_ranges != 'None' else 'None'}")}
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        <div>Visual Diff Evidence</div>
        <div class="muted">Only pages with warned/major visual deltas or flagged decisions are listed.</div>
      </div>

      {_render_simple_table(
          ["Page", "Severity", "Visual Engine Note", "Similarity", "Snapshot"],
          visual_rows,
          min_width=900
      )}
    </div>

    <div class="card">
      <div class="section-title">
        <div>Structured Findings</div>
        <div class="muted">{'Suppressed because LLM classification failed.' if llm_failed else 'Detailed model findings.'}</div>
      </div>

      {("" if show_llm_detail else "<div class='banner'>Detailed finding tables are hidden because the LLM classification step failed. Raw page decisions above are safer to use for this run.</div>")}

      {(_render_detail_table(
          mismatches,
          "Value Mismatches",
          [("page", "Page"), ("severity", "Severity"), ("field", "Field"), ("expected", "Expected"), ("actual", "Actual"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          missing,
          "Missing Content",
          [("page", "Page"), ("severity", "Severity"), ("field", "Field"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          extra,
          "Extra Content",
          [("page", "Page"), ("severity", "Severity"), ("field", "Field"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          layout,
          "Layout Anomalies",
          [("page", "Page"), ("severity", "Severity"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          fmt_issues,
          "Format Issues",
          [("page", "Page"), ("severity", "Severity"), ("field", "Field"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          spelling,
          "Spelling Errors",
          [("page", "Page"), ("severity", "Severity"), ("description", "Description")]
      ) if show_llm_detail else "")}

      {(_render_detail_table(
          compliance,
          "Compliance Issues",
          [("page", "Page"), ("severity", "Severity"), ("description", "Description")]
      ) if show_llm_detail else "")}
    </div>

  </div>
</body>
</html>
"""

    out_path.write_text(html, encoding="utf-8")
    return filename