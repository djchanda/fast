# app/reporting/html_report.py
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import current_app, url_for


# -----------------------
# Paths
# -----------------------
def _reports_dir() -> Path:
    d = Path(current_app.instance_path) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# -----------------------
# Basic helpers
# -----------------------
def _safe_slug(s: str) -> str:
    s = (s or "test").strip()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    return (s[:60] or "test")


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


def _chip(text: str, kind: str) -> str:
    cls = {
        "ok": "chip chip-ok",
        "warn": "chip chip-warn",
        "bad": "chip chip-bad",
        "info": "chip chip-info",
    }.get(kind, "chip chip-info")
    return f"<span class='{cls}'>{_esc(text)}</span>"


def _kv_row(label: str, value_html: str) -> str:
    return f"""
      <div class="kv">
        <div class="kv-k">{_esc(label)}</div>
        <div class="kv-v">{value_html}</div>
      </div>
    """


def _stringify_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return str(v)


def _pick_first(d: dict, keys: list[str], default=""):
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def _extract_page(it: dict) -> str:
    # direct keys
    v = _pick_first(it, ["page", "page_num", "page_no", "page_number", "pageno", "pg", "pageIndex"])
    if v not in ("", None):
        return str(v)

    # nested location
    loc = it.get("location") or it.get("loc") or {}
    if isinstance(loc, dict):
        v = _pick_first(loc, ["page", "page_num", "page_no", "page_number", "pageno", "pg"])
        if v not in ("", None):
            return str(v)

    # nested source
    src = it.get("source") or {}
    if isinstance(src, dict):
        v = _pick_first(src, ["page", "page_num", "page_number"])
        if v not in ("", None):
            return str(v)

    return ""


def _extract_field(it: dict) -> str:
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
            "xpath",
            "json_path",
            "jsonPath",
        ],
    )
    if v not in ("", None):
        return str(v)

    # Sometimes nested: {"field": {"name": "..."}}
    f = it.get("field")
    if isinstance(f, dict):
        v = _pick_first(f, ["name", "label", "key", "field_name"])
        if v not in ("", None):
            return str(v)

    return ""


# -----------------------
# Rendering helpers (tables)
# -----------------------
def _render_table(items: Any, columns: list[tuple[str, str]]) -> str:
    """
    items: list[dict] expected, but safe even if odd-shaped
    columns: [(json_key, "Header"), ...]
    Special keys supported: "page" and "field" use extractors.
    """
    items = _as_list(items)
    if not items:
        return "<div class='muted'>None</div>"

    # If LLM returned strings or something odd
    if not any(isinstance(x, dict) for x in items):
        out = ["<ul class='bullets'>"]
        for it in items:
            out.append(f"<li>{_esc(_stringify_cell(it))}</li>")
        out.append("</ul>")
        return "\n".join(out)

    thead = "".join(f"<th>{_esc(h)}</th>" for _, h in columns)
    rows = []

    for it in items:
        if not isinstance(it, dict):
            vals = ["" for _ in columns]
            vals[-1] = _stringify_cell(it)
            rows.append("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in vals) + "</tr>")
            continue

        tds = []
        for key, _hdr in columns:
            if key == "page":
                v = _extract_page(it)
            elif key == "field":
                v = _extract_field(it)
            else:
                v = it.get(key, "")

                # Helpful aliases
                if v == "" and key == "description":
                    v = it.get("note") or it.get("reason") or it.get("details") or ""
                if v == "" and key == "expected":
                    v = it.get("expected_value") or it.get("expectedValue") or it.get("exp") or ""
                if v == "" and key == "actual":
                    v = it.get("actual_value") or it.get("actualValue") or it.get("act") or ""

            tds.append(f"<td>{_esc(_stringify_cell(v))}</td>")

        rows.append("<tr>" + "".join(tds) + "</tr>")

    rows_html = "".join(rows) if rows else f"<tr><td colspan='{len(columns)}' class='muted'>None</td></tr>"

    return f"""
      <div class="table-wrap">
        <table class="fast-table">
          <thead><tr>{thead}</tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    """


# -----------------------
# Main report writer
# -----------------------
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
    """
    Writes HTML report file into instance/reports and returns filename ONLY.
    Filename: <testname>_YYYYMMDD_HHMMSS.html
    No raw JSON debug.
    Tables for findings (Expected vs Actual).
    Sticky headers + modern ALM look.
    """

    # Friendly filename
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tc_name = _safe_slug(getattr(tc, "name", "test"))
    filename = f"{tc_name}_{ts}.html"
    out_path = _reports_dir() / filename

    mode = (result_json.get("mode") or getattr(tc, "mode", "") or "").strip().lower()

    # Safe list extraction
    spelling = _as_list(result_json.get("spelling_errors"))
    fmt_issues = _as_list(result_json.get("format_issues"))
    mism = _as_list(result_json.get("value_mismatches"))
    missing = _as_list(result_json.get("missing_content"))
    extra = _as_list(result_json.get("extra_content"))
    layout = _as_list(result_json.get("layout_anomalies"))
    compliance = _as_list(result_json.get("compliance_issues"))
    visual = _as_list(result_json.get("visual_validation"))

    overall_summary = (result_json.get("overall_summary") or llm_summary or "").strip()
    error_msg = (result_json.get("error") or "").strip()

    # Verdict
    if error_msg:
        verdict = _chip("FAIL", "bad")
    else:
        verdict = _chip("PASS", "ok") if "fail" not in overall_summary.lower() else _chip("FAIL", "bad")

    # Visual diff rows
    visual_rows = ""
    for v in (visual or []):
        page = ""
        sim = ""
        note = ""
        link = "—"
        kind = "info"

        if isinstance(v, dict):
            page = v.get("page", "")
            sim = v.get("similarity", "")
            note = v.get("note", "")

            if v.get("major"):
                kind = "bad"
            elif v.get("warn"):
                kind = "warn"
            else:
                kind = "ok"

            snap = v.get("snapshot_path")
            if snap:
                img_name = str(snap).split("/")[-1]
                href = url_for(
                    "web.serve_visual_diff_file",
                    project_id=project_id,
                    filename=img_name,
                    _external=True,
                )
                link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>View</a>"
        else:
            note = f"Unexpected visual diff row type: {type(v).__name__}"

        sim_text = f"{float(sim) * 100:.3f}" if isinstance(sim, (float, int)) else (str(sim) if sim != "" else "N/A")
        visual_rows += f"""
          <tr>
            <td>{_esc(page)}</td>
            <td>{_chip(sim_text + (' FAIL' if kind == 'bad' else ''), kind)}</td>
            <td>{_esc(note)}</td>
            <td>{link}</td>
          </tr>
        """

    if not visual_rows:
        visual_rows = """
          <tr>
            <td colspan="4" class="muted">No visual diff output (run Benchmark mode to generate visual comparison).</td>
          </tr>
        """

    # PDF links
    main_pdf_link = ""
    bench_pdf_link = ""
    if main_form and getattr(main_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=main_form.id, _external=True)
        main_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"
    if bench_form and getattr(bench_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=bench_form.id, _external=True)
        bench_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"

    # ---- Build HTML ----
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FAST Report - {_esc(tc_name)}</title>
  <style>
    :root{{
      --bg:#f7f8fa;
      --card:#ffffff;
      --text:#111827;
      --muted:#6b7280;
      --border:#e5e7eb;
      --shadow2: 0 2px 10px rgba(17,24,39,.05);

      --ok-bg:#ecfdf3; --ok-b:#b7f0c8; --ok-t:#0f7a3a;
      --warn-bg:#fff7ed; --warn-b:#fed7aa; --warn-t:#9a3412;
      --bad-bg:#fff1f2; --bad-b:#fecdd3; --bad-t:#9f1239;
      --info-bg:#eff6ff; --info-b:#bfdbfe; --info-t:#1d4ed8;
    }}

    html,body{{ background:var(--bg); }}
    body{{
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
      margin: 0;
      color: var(--text);
    }}

    .wrap{{
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px;
    }}

    .topbar{{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow2);
      padding: 14px 16px;
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap: 14px;
    }}

    h1{{
      font-size: 18px;
      margin: 0 0 6px 0;
      letter-spacing: .2px;
    }}

    .muted{{ color: var(--muted); font-size: 13px; }}
    .mono{{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}

    .card{{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow2);
      padding: 14px;
      margin-top: 14px;
    }}

    .grid{{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 18px;
    }}

    .kv{{
      display:flex;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px dashed #eef0f3;
    }}
    .kv:last-child{{ border-bottom:none; }}
    .kv-k{{ width: 180px; color: var(--muted); }}
    .kv-v{{ flex:1; }}

    .chip{{
      display:inline-flex;
      align-items:center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid transparent;
      line-height: 1;
      letter-spacing: .2px;
    }}
    .chip-ok{{ background:var(--ok-bg); border-color:var(--ok-b); color:var(--ok-t); }}
    .chip-warn{{ background:var(--warn-bg); border-color:var(--warn-b); color:var(--warn-t); }}
    .chip-bad{{ background:var(--bad-bg); border-color:var(--bad-b); color:var(--bad-t); }}
    .chip-info{{ background:var(--info-bg); border-color:var(--info-b); color:var(--info-t); }}

    .section-title{{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap: 10px;
      margin: 0;
      font-size: 14px;
    }}

    .table-wrap{{
      overflow:auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      margin-top: 10px;
      background: #fff;
    }}
    .fast-table{{
      width:100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 820px;
    }}
    .fast-table th, .fast-table td{{
      border-bottom: 1px solid #eef0f3;
      padding: 10px 10px;
      font-size: 13px;
      vertical-align: top;
      text-align:left;
      white-space: normal;
    }}
    .fast-table th{{
      background: #f9fafb;
      position: sticky;
      top: 0;
      z-index: 2;
      font-size: 12px;
      color: #374151;
      text-transform: uppercase;
      letter-spacing: .4px;
    }}
    .fast-table tbody tr:nth-child(odd){{ background:#ffffff; }}
    .fast-table tbody tr:nth-child(even){{ background:#fbfcfe; }}
    .fast-table tbody tr:hover{{ background:#f1f5f9; }}

    a{{ color:#2563eb; text-decoration:none; }}
    a:hover{{ text-decoration: underline; }}

    @media (max-width: 920px){{
      .grid{{ grid-template-columns: 1fr; }}
      .kv-k{{ width: 140px; }}
      .fast-table{{ min-width: 680px; }}
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
        {_kv_row("Main Form", _esc(getattr(main_form, "original_filename", "") or getattr(main_form, "stored_filename", "") or "") + (" &nbsp;|&nbsp; " + main_pdf_link if main_pdf_link else ""))}
        {_kv_row("Benchmark Form", _esc(getattr(bench_form, "original_filename", "") or getattr(bench_form, "stored_filename", "") or "") + (" &nbsp;|&nbsp; " + bench_pdf_link if bench_pdf_link else ""))}
        {_kv_row("Generated", _esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))}
        {_kv_row("Report File", f"<span class='mono'>{_esc(filename)}</span>")}
      </div>
    </div>

    <div class="card">
      <div class="section-title"><div>Summary</div></div>
      <div style="margin-top:8px;">{_esc(overall_summary) if overall_summary else "<span class='muted'>No summary provided.</span>"}</div>
      {f"<div class='chip chip-bad' style='margin-top:10px;'>ERROR: {_esc(error_msg)}</div>" if error_msg else ""}
    </div>

    <div class="card">
      <div class="section-title"><div>Findings</div></div>

      <div class="grid" style="margin-top: 10px;">
        {_kv_row("Spelling Errors", _chip(str(len(spelling)), "bad" if len(spelling) else "ok"))}
        {_kv_row("Format Issues", _chip(str(len(fmt_issues)), "bad" if len(fmt_issues) else "ok"))}
        {_kv_row("Value Mismatches", _chip(str(len(mism)), "bad" if len(mism) else "ok"))}
        {_kv_row("Missing Content", _chip(str(len(missing)), "bad" if len(missing) else "ok"))}
        {_kv_row("Extra Content", _chip(str(len(extra)), "warn" if len(extra) else "ok"))}
        {_kv_row("Layout Anomalies", _chip(str(len(layout)), "warn" if len(layout) else "ok"))}
        {_kv_row("Compliance Issues", _chip(str(len(compliance)), "bad" if len(compliance) else "ok"))}
      </div>

      <div style="margin-top: 12px;">
        <div class="section-title"><div>Details</div></div>

        <div class="card" style="margin-top:10px;">
          <b>Value Mismatches</b>
          {_render_table(mism, [("page","Page"), ("field","Field"), ("expected","Expected"), ("actual","Actual"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Format Issues</b>
          {_render_table(fmt_issues, [("page","Page"), ("field","Field"), ("expected","Expected"), ("actual","Actual"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Spelling Errors</b>
          {_render_table(spelling, [("page","Page"), ("text","Text"), ("suggestion","Suggestion"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Missing Content</b>
          {_render_table(missing, [("page","Page"), ("field","Field"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Extra Content</b>
          {_render_table(extra, [("page","Page"), ("field","Field"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Layout Anomalies</b>
          {_render_table(layout, [("page","Page"), ("description","Note")])}
        </div>

        <div class="card">
          <b>Compliance Issues</b>
          {_render_table(compliance, [("page","Page"), ("rule","Rule"), ("description","Note")])}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        <div>Layout & Visual Differences</div>
        <div>{_chip("FAIL" if any(isinstance(v, dict) and v.get("major") for v in visual) else "PASS",
                   "bad" if any(isinstance(v, dict) and v.get("major") for v in visual) else "ok")}</div>
      </div>

      <div class="table-wrap">
        <table class="fast-table" style="min-width: 640px;">
          <thead>
            <tr>
              <th style="width:80px;">Page</th>
              <th style="width:200px;">Similarity</th>
              <th>Note</th>
              <th style="width:120px;">Snapshot</th>
            </tr>
          </thead>
          <tbody>
            {visual_rows}
          </tbody>
        </table>
      </div>
    </div>

    <!-- No Debug JSON -->
  </div>
</body>
</html>
"""

    out_path.write_text(html, encoding="utf-8")
    return filename
