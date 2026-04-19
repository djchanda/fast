from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import current_app, url_for


def _reports_dir() -> Path:
    d = Path(current_app.instance_path) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def _chip(text: str, kind: str) -> str:
    cls = {
        "ok": "chip chip-ok",
        "warn": "chip chip-warn",
        "bad": "chip chip-bad",
        "info": "chip chip-info",
        "neutral": "chip chip-neutral",
    }.get(kind, "chip chip-info")
    return f"<span class='{cls}'>{_esc(text)}</span>"


def _status_chip(status: str) -> str:
    s = (status or "").lower()
    if s == "match":
        return _chip("MATCH", "ok")
    if s == "review":
        return _chip("REVIEW", "warn")
    if s == "mismatch":
        return _chip("MISMATCH", "bad")
    return _chip(status.upper(), "neutral")


def _severity_chip(sev: str) -> str:
    s = (sev or "").lower()
    if s in ("critical", "high"):
        return _chip(s.upper(), "bad")
    if s in ("medium", "warn", "warning"):
        return _chip(s.upper(), "warn")
    if s in ("low", "info"):
        return _chip(s.upper(), "info")
    return _chip((sev or "INFO").upper(), "neutral")


def _pick_first(d: dict, keys: list[str], default=""):
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def _extract_page(it: dict) -> Optional[int]:
    if not isinstance(it, dict):
        return None
    for k in ["page", "page_num", "page_no", "page_number", "pageno", "pg"]:
        if it.get(k) not in (None, ""):
            try:
                return int(str(it.get(k)).strip())
            except Exception:
                return None
    return None


def _extract_field(it: dict) -> str:
    if not isinstance(it, dict):
        return ""
    return str(
        _pick_first(
            it,
            ["field", "field_name", "fieldName", "name", "label", "key", "category"],
            default="",
        )
    )


def _shorten(text: Any, n: int = 260) -> str:
    s = str(text or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


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


def _llm_failed(result_json: Dict[str, Any]) -> bool:
    err = str(result_json.get("error") or "").lower()
    return bool(err and ("gemini" in err or "llm" in err or "failed" in err))


def _collect_issue_rows(result_json: Dict[str, Any]) -> List[dict]:
    buckets = [
        ("value_mismatches", "Value difference", "critical"),
        ("missing_content", "Missing content", "high"),
        ("extra_content", "Extra content", "medium"),
        ("layout_anomalies", "Layout difference", "medium"),
        ("format_issues", "Format issue", "low"),
        ("spelling_errors", "Spelling issue", "low"),
        ("compliance_issues", "Compliance issue", "high"),
        ("visual_mismatches", "Visual difference", "high"),
    ]

    rows = []
    for bucket, default_category, default_sev in buckets:
        for it in _as_list(result_json.get(bucket)):
            if not isinstance(it, dict):
                continue
            page = _extract_page(it)
            rows.append(
                {
                    "page": page,
                    "bucket": bucket,
                    "category": str(it.get("category") or default_category),
                    "severity": str(it.get("severity") or default_sev).lower(),
                    "field": _extract_field(it),
                    "description": str(
                        _pick_first(it, ["description", "details", "reason", "note", "evidence"], default=default_category)
                    ),
                    "raw": it,
                }
            )
    return rows


def _group_by_page(rows: List[dict]) -> Dict[int, List[dict]]:
    d = defaultdict(list)
    for r in rows:
        if r.get("page") is not None:
            d[int(r["page"])].append(r)
    return d


def _page_decisions(result_json: Dict[str, Any]) -> List[dict]:
    issues = _collect_issue_rows(result_json)
    by_page = _group_by_page(issues)
    visual = _as_list(result_json.get("visual_validation"))

    pages = set(by_page.keys())
    for v in visual:
        if isinstance(v, dict):
            p = _extract_page(v)
            if p is not None:
                pages.add(p)

    decisions = []

    for page in sorted(pages):
        page_issues = by_page.get(page, [])
        page_visuals = [v for v in visual if isinstance(v, dict) and _extract_page(v) == page]

        status = "match"
        severity = "low"
        decision = "All matching."
        evidence = []
        similarity = "—"
        snapshot = ""

        if page_issues:
            strongest = sorted(
                page_issues,
                key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 9),
            )[0]

            severity = strongest["severity"]
            decision = strongest["description"]
            evidence.extend([r["description"] for r in page_issues[:4]])

            if strongest["bucket"] in ("value_mismatches", "missing_content", "extra_content", "compliance_issues", "visual_mismatches"):
                status = "mismatch"
            else:
                status = "review"

        if page_visuals:
            pv = sorted(
                page_visuals,
                key=lambda x: (0 if x.get("signature_candidate") else 1 if x.get("major") else 2 if x.get("warn") else 3),
            )[0]

            sim = pv.get("similarity")
            if isinstance(sim, (int, float)):
                similarity = f"{float(sim) * 100:.3f}%"

            snapshot = str(pv.get("snapshot_path") or "")

            if pv.get("signature_candidate") and status != "mismatch":
                status = "review"
                severity = "medium"
                decision = pv.get("signature_reason") or "Signature-region change detected."
                evidence.append(decision)

            elif pv.get("major") and status == "match":
                status = "review"
                severity = "medium"
                decision = str(pv.get("note") or "Significant visual difference detected.")
                evidence.append(decision)

            elif pv.get("warn") and status == "match":
                status = "review"
                severity = "low"
                decision = str(pv.get("note") or "Visual difference detected.")
                evidence.append(decision)

        if status == "match":
            severity = "low"
            decision = "All matching."
            evidence = []

        decisions.append(
            {
                "page": page,
                "status": status,
                "severity": severity,
                "decision": decision,
                "evidence": " | ".join(dict.fromkeys([e for e in evidence if e]))[:900],
                "similarity": similarity,
                "snapshot": snapshot,
            }
        )

    return decisions


def _compress_pages(pages: List[int]) -> str:
    if not pages:
        return "None"
    pages = sorted(pages)
    ranges = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
        else:
            ranges.append((start, prev))
            start = prev = p
    ranges.append((start, prev))
    parts = []
    for a, b in ranges:
        parts.append(str(a) if a == b else f"{a}-{b}")
    return ", ".join(parts)


def _render_table(headers: List[str], body_html: str) -> str:
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    return f"""
      <div class="table-wrap">
        <table class="fast-table">
          <thead><tr>{thead}</tr></thead>
          <tbody id="decision-table-body">{body_html}</tbody>
        </table>
      </div>
    """


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

    mode = (result_json.get("mode") or getattr(tc, "mode", "") or "").strip().lower()
    llm_failed = _llm_failed(result_json)

    decisions = _page_decisions(result_json)
    mismatches = [d for d in decisions if d["status"] == "mismatch"]
    reviews = [d for d in decisions if d["status"] == "review"]
    matches = [d for d in decisions if d["status"] == "match"]

    matched_ranges = _compress_pages([d["page"] for d in matches])

    # ── Page-structure change summary (deleted / inserted pages) ────────────
    visual_rows = _as_list(result_json.get("visual_validation"))
    deleted_pages = [
        v for v in visual_rows
        if isinstance(v, dict) and str(v.get("alignment_op") or "") == "deleted"
    ]
    inserted_pages = [
        v for v in visual_rows
        if isinstance(v, dict) and str(v.get("alignment_op") or "") == "inserted"
    ]
    page_structure_banner = ""
    if deleted_pages or inserted_pages:
        parts = []
        if deleted_pages:
            pg_nums = ", ".join(
                str(v.get("expected_page_num") or v.get("page", "?"))
                for v in deleted_pages
            )
            parts.append(
                f"<strong>{len(deleted_pages)} page(s) removed</strong> "
                f"from expected PDF (expected page(s): {_esc(pg_nums)})"
            )
        if inserted_pages:
            pg_nums = ", ".join(
                str(v.get("actual_page_num") or v.get("page", "?"))
                for v in inserted_pages
            )
            parts.append(
                f"<strong>{len(inserted_pages)} page(s) inserted</strong> "
                f"in actual PDF (actual page(s): {_esc(pg_nums)})"
            )
        page_structure_banner = (
            "<div style='background:rgba(217,119,6,0.12);border-left:4px solid #d97706;"
            "padding:12px 16px;border-radius:4px;margin-bottom:16px;font-size:13px;'>"
            "<strong style='color:#fbbf24;'>&#9888; Page-Structure Change Detected</strong><br/>"
            + " &mdash; ".join(parts)
            + "<br/><span style='color:#fbbf24;font-size:12px;'>The sequence-alignment engine "
            "re-aligned remaining pages so only true content differences are reported below.</span>"
            "</div>"
        )

    if llm_failed:
        summary_text = (
            "LLM classification failed. The page-by-page decision below is based on deterministic visual and structured evidence."
        )
    else:
        summary_text = (
            result_json.get("overall_summary")
            or llm_summary
            or f"Found {len(mismatches)} mismatch page(s) and {len(reviews)} review page(s)."
        )

    if mismatches or reviews:
        verdict = _chip("IN REVIEW", "warn")
    else:
        verdict = _chip("PASS", "ok")

    main_pdf_link = ""
    bench_pdf_link = ""
    if main_form and getattr(main_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=main_form.id, _external=True)
        main_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"
    if bench_form and getattr(bench_form, "id", None):
        href = url_for("web.view_form_file", project_id=project_id, form_id=bench_form.id, _external=True)
        bench_pdf_link = f"<a href='{_esc(href)}' target='_blank' rel='noopener'>Open PDF</a>"

    if mismatches or reviews:
        decision_rows_html = []
        for d in [*mismatches, *reviews]:
            row_status = (d["status"] or "").lower()
            decision_rows_html.append(
                f"""
                <tr data-status="{_esc(row_status)}">
                    <td>{_esc(str(d["page"]))}</td>
                    <td>{_status_chip(d["status"])}</td>
                    <td>{_severity_chip(d["severity"])}</td>
                    <td>{_esc(_shorten(d["decision"], 240))}</td>
                    <td>{_esc(_shorten(d["evidence"], 460))}</td>
                    <td>{_esc(d["similarity"])}</td>
                    <td>{_snapshot_link(project_id, d["snapshot"]) if d["snapshot"] else "—"}</td>
                </tr>
                """
            )
        decision_rows_html = "".join(decision_rows_html)
    else:
        decision_rows_html = "<tr><td colspan='7' class='muted'>None</td></tr>"

    page_decision_table = f"""
    <div class="card">
      <div class="section-title">
        <div>Page-by-Page Decision</div>
        <div class="muted">Filter by decision type.</div>
      </div>

      {page_structure_banner}

      <div class="rd-filterbar">
        <button class="rd-filter-btn active" onclick="filterDecisionRows('all', this)">All</button>
        <button class="rd-filter-btn" onclick="filterDecisionRows('mismatch', this)">Mismatch</button>
        <button class="rd-filter-btn" onclick="filterDecisionRows('review', this)">Review</button>
      </div>

      {_render_table(
          ["Page", "Status", "Severity", "Decision", "Evidence", "Similarity", "Snapshot"],
          decision_rows_html
      )}

      <div style="margin-top:10px;" class="muted">
        {("All pages matched." if not mismatches and not reviews else f"All other pages matched: {matched_ranges if matched_ranges != 'None' else 'None'}")}
      </div>
    </div>
    """

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FAST Report - {_esc(tc_name)}</title>
  <style>
    :root {{
      --bg:#0f1623;
      --card:#141c2d;
      --card2:#1a2236;
      --text:#e2e8f0;
      --muted:#94a3b8;
      --border:rgba(255,255,255,0.08);
      --border2:rgba(255,255,255,0.05);
      --shadow:0 2px 10px rgba(0,0,0,0.4);

      --ok-bg:rgba(22,163,74,0.15);   --ok-b:rgba(22,163,74,0.3);   --ok-t:#4ade80;
      --warn-bg:rgba(217,119,6,0.15); --warn-b:rgba(217,119,6,0.3); --warn-t:#fbbf24;
      --bad-bg:rgba(220,38,38,0.15);  --bad-b:rgba(220,38,38,0.3);  --bad-t:#f87171;
      --info-bg:rgba(2,132,199,0.15); --info-b:rgba(2,132,199,0.3); --info-t:#60a5fa;
      --neutral-bg:rgba(255,255,255,0.07); --neutral-b:rgba(255,255,255,0.12); --neutral-t:#cbd5e1;
    }}

    * {{ box-sizing: border-box; }}

    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }}

    .wrap {{
      width: 100%;
      max-width: none;
      padding: 16px 18px 28px;
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
      margin-bottom: 14px;
    }}

    .card {{
      padding: 16px;
      width: 100%;
      margin-bottom: 14px;
    }}

    h1 {{
      margin: 0 0 6px 0;
      font-size: 20px;
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
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px 20px;
    }}

    .kv {{
      display:flex;
      gap:12px;
      padding:8px 0;
      border-bottom:1px dashed var(--border2);
    }}
    .kv:last-child {{ border-bottom:none; }}

    .kv-k {{
      width:160px;
      color:var(--muted);
      font-size:13px;
      flex: 0 0 160px;
    }}

    .kv-v {{
      flex:1;
      font-size:13px;
      word-break: break-word;
    }}

    .section-title {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:10px;
      margin:0 0 10px 0;
      font-size:15px;
      font-weight:700;
    }}

    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      overflow-y: visible;
      border:1px solid var(--border);
      border-radius:12px;
      background:var(--card);
      margin-top:8px;
    }}

    .fast-table {{
      width: 100%;
      min-width: 1450px;
      border-collapse: separate;
      border-spacing: 0;
      table-layout: fixed;
    }}

    .fast-table th,
    .fast-table td {{
      padding: 10px 12px;
      border-bottom:1px solid var(--border);
      text-align:left;
      vertical-align:top;
      font-size:13px;
      white-space: normal;
      word-break: break-word;
    }}

    .fast-table th {{
      background:var(--card2);
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.4px;
      color:var(--muted);
      position: sticky;
      top: 0;
      z-index: 2;
    }}

    .fast-table th:nth-child(1), .fast-table td:nth-child(1) {{ width: 70px; }}
    .fast-table th:nth-child(2), .fast-table td:nth-child(2) {{ width: 110px; }}
    .fast-table th:nth-child(3), .fast-table td:nth-child(3) {{ width: 110px; }}
    .fast-table th:nth-child(4), .fast-table td:nth-child(4) {{ width: 360px; }}
    .fast-table th:nth-child(5), .fast-table td:nth-child(5) {{ width: 470px; }}
    .fast-table th:nth-child(6), .fast-table td:nth-child(6) {{ width: 110px; }}
    .fast-table th:nth-child(7), .fast-table td:nth-child(7) {{ width: 100px; }}

    .banner {{
      margin-top:12px;
      padding:12px 14px;
      border-radius:12px;
      border:1px solid var(--warn-b);
      background:var(--warn-bg);
      color:var(--warn-t);
      font-size:13px;
    }}

    .rd-filterbar {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}

    .rd-filter-btn {{
      border: 1px solid var(--border);
      background: var(--card2);
      color: var(--text);
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }}

    .rd-filter-btn:hover {{
      background: rgba(255,255,255,0.1);
    }}

    .rd-filter-btn.active {{
      background: var(--info-bg);
      border-color: var(--info-b);
      color: var(--info-t);
    }}

    a {{
      color:#60a5fa;
      text-decoration:none;
    }}
    a:hover {{
      text-decoration:underline;
    }}

    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}

    @media (max-width: 1100px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
      .kv-k {{
        width:135px;
        flex-basis: 135px;
      }}
      .fast-table {{
        min-width: 1150px;
      }}
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
      <div class="section-title">Execution Summary</div>
      <div>{_esc(summary_text)}</div>

      {f"<div class='banner'><b>LLM classification unavailable.</b> {_esc(result_json.get('error'))}{(': ' + _esc(result_json.get('details'))) if result_json.get('details') else ''}</div>" if llm_failed else ""}

      <div style="margin-top:12px; display:flex; flex-wrap:wrap; gap:8px;">
        {_chip(f"Mismatches: {len(mismatches)}", "bad" if mismatches else "ok")}
        {_chip(f"Review pages: {len(reviews)}", "warn" if reviews else "ok")}
        {_chip(f"Matched pages: {len(matches)}", "ok")}
        {_chip(f"Matched page ranges: {matched_ranges}", "info" if matched_ranges != 'None' else "neutral")}
      </div>
    </div>

    <div class="card">
      <div class="section-title">HTML Report</div>
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

    {page_decision_table}

  <script>
    function filterDecisionRows(status, btn) {{
      const rows = document.querySelectorAll('#decision-table-body tr[data-status]');
      rows.forEach(row => {{
        if (status === 'all' || row.dataset.status === status) {{
          row.style.display = '';
        }} else {{
          row.style.display = 'none';
        }}
      }});

      document.querySelectorAll('.rd-filter-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
    }}
  </script>

  </div>
</body>
</html>
"""

    out_path.write_text(html, encoding="utf-8")
    return filename