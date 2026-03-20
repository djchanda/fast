"""
Evidence bundle export — creates a ZIP containing:
  - Tested PDFs
  - Visual diff images
  - HTML report
  - Raw JSON findings
  - Audit log CSV
  - Manifest JSON
"""
from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime
from typing import Optional


def build_evidence_bundle(run_id: int, project_id: int) -> bytes:
    """
    Build a ZIP evidence bundle for a run and return raw bytes.

    Args:
        run_id: The Run.id
        project_id: The Project.id

    Returns:
        bytes of the ZIP archive.
    """
    from flask import current_app
    from app.models.run import Run
    from app.models.run_result import RunResult
    from app.models.test_case import TestCase
    from app.models.form import Form
    from app.models.audit_log import AuditLog

    run = Run.query.get(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")

    results = RunResult.query.filter_by(run_id=run_id).all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

        # --- Manifest ---
        manifest = {
            "bundle_version": "1.0",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "project_id": project_id,
            "run_id": run_id,
            "run_status": run.status,
            "triggered_by": run.triggered_by,
            "total_test_cases": run.total,
            "total_errors": run.errors,
            "total_warnings": run.warnings,
            "total_passed": run.passed,
            "results": [],
        }

        for rr in results:
            tc = TestCase.query.get(rr.test_case_id)
            tc_name = tc.name if tc else f"tc_{rr.test_case_id}"

            entry = {
                "result_id": rr.id,
                "test_case": tc_name,
                "mode": rr.mode,
                "status": rr.status,
                "errors": rr.errors,
                "warnings": rr.warnings,
                "passed": rr.passed,
            }
            manifest["results"].append(entry)

            # --- JSON findings ---
            if rr.result_json:
                zf.writestr(
                    f"results/result_{rr.id}_{tc_name}/findings.json",
                    rr.result_json,
                )

            # --- HTML report ---
            if rr.report_html_path:
                report_path = (
                    os.path.join(current_app.instance_path, "reports", rr.report_html_path)
                )
                if os.path.exists(report_path):
                    with open(report_path, "rb") as f:
                        zf.writestr(f"results/result_{rr.id}_{tc_name}/report.html", f.read())

            # --- Tested PDF ---
            if tc and tc.form_id:
                form = Form.query.get(tc.form_id)
                if form and form.file_path and os.path.exists(form.file_path):
                    with open(form.file_path, "rb") as f:
                        zf.writestr(
                            f"results/result_{rr.id}_{tc_name}/tested_form.pdf", f.read()
                        )

            # --- Benchmark PDF ---
            if tc and tc.benchmark_form_id:
                bench_form = Form.query.get(tc.benchmark_form_id)
                if bench_form and bench_form.file_path and os.path.exists(bench_form.file_path):
                    with open(bench_form.file_path, "rb") as f:
                        zf.writestr(
                            f"results/result_{rr.id}_{tc_name}/golden_copy.pdf", f.read()
                        )

            # --- Visual diff images ---
            if rr.visual_diff_images:
                try:
                    diff_images = json.loads(rr.visual_diff_images)
                    vdiffs_dir = os.path.join(current_app.instance_path, "visual_diffs")
                    for img_name in diff_images:
                        img_path = os.path.join(vdiffs_dir, img_name)
                        if os.path.exists(img_path):
                            with open(img_path, "rb") as f:
                                zf.writestr(
                                    f"results/result_{rr.id}_{tc_name}/visual_diffs/{img_name}",
                                    f.read(),
                                )
                except Exception:
                    pass

        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # --- Audit log CSV ---
        audit_entries = AuditLog.query.filter_by(project_id=project_id).order_by(
            AuditLog.created_at.desc()
        ).limit(500).all()

        if audit_entries:
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerow(["id", "action", "resource_type", "resource_id", "username", "user_ip", "created_at", "detail"])
            for a in audit_entries:
                writer.writerow([a.id, a.action, a.resource_type, a.resource_id, a.username, a.user_ip, a.created_at.isoformat(), a.detail])
            zf.writestr("audit_log.csv", csv_buf.getvalue())

    buf.seek(0)
    return buf.read()
