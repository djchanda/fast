# app/routes/web.py
import io
import os
import json
from pathlib import Path
from typing import Optional

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    current_app,
    send_from_directory,
    send_file,
    abort, jsonify,
)
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.project import Project
from app.models.form import Form
from app.models.test_case import TestCase
from app.models.run import Run
from app.models.run_result import RunResult
from app.models.user import User
from app.models.audit_log import AuditLog
from app.models.finding_review import FindingReview
from app.models.finding_comment import FindingComment
from app.models.approval_gate import ApprovalGate
from app.models.webhook_config import WebhookConfig
from app.models.scheduled_run import ScheduledRun
from app.models.compliance_standard import ComplianceStandard, ComplianceRequirement
from app.models.false_positive import FalsePositive
from app.models.field_inventory import FieldInventory
from app.models.api_key import ApiKey
from app.services.runner import run_testcase
from app.services.audit import log_action


web_bp = Blueprint("web", __name__)

ALLOWED_EXTENSIONS = {"pdf"}
MAX_FORMS_PER_PROJECT = 10


# -----------------------
# Helpers
# -----------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def project_forms_dir(project_id: int) -> str:
    base = os.path.join(current_app.instance_path, "uploads", f"project_{project_id}", "forms")
    os.makedirs(base, exist_ok=True)
    return base


def reports_dir() -> Path:
    d = Path(current_app.instance_path) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def visual_diffs_dir() -> Path:
    d = Path(current_app.instance_path) / "visual_diffs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_logged_in() -> bool:
    return bool(session.get("user"))


def require_login() -> Optional[object]:
    if not is_logged_in():
        return redirect(url_for("web.landing"))
    return None


# -----------------------
# Auth / Landing
# -----------------------
@web_bp.route("/", methods=["GET", "POST"])
def landing():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not username or not password:
            flash("Please enter username and password.", "error")
            return redirect(url_for("web.landing"))

        # Real authentication against users table
        user = User.query.filter_by(username=username, is_active=True).first()
        if user and user.check_password(password):
            from datetime import datetime
            user.last_login = datetime.utcnow()
            db.session.commit()
            session["user"] = username
            session["role"] = user.role
            session["user_id"] = user.id
            log_action("auth.login", resource_type="user", resource_id=user.id)
            return redirect(url_for("web.home"))
        else:
            flash("Invalid username or password.", "error")
            log_action("auth.login_failed", detail={"username": username})
            return redirect(url_for("web.landing"))

    if is_logged_in():
        return redirect(url_for("web.home"))

    return render_template("landing.html", page_title="FAST | Login")


@web_bp.route("/logout")
def logout():
    log_action("auth.logout")
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("web.landing"))


def require_role(min_role: str):
    """Return a redirect if user doesn't have at least the required role."""
    role_rank = {"viewer": 0, "reviewer": 1, "admin": 2}
    user_role = session.get("role", "viewer")
    if role_rank.get(user_role, 0) < role_rank.get(min_role, 0):
        flash(f"Access denied: requires '{min_role}' role.", "error")
        return redirect(url_for("web.home"))
    return None


# -----------------------
# Static serving for generated artifacts
# -----------------------
@web_bp.route("/projects/<int:project_id>/visual_diffs/<path:filename>", methods=["GET"])
def serve_visual_diff_file(project_id: int, filename: str):
    """
    Serve visual diff PNGs from instance/visual_diffs safely (no traversal).
    """
    gate = require_login()
    if gate:
        return gate

    safe_name = os.path.basename(filename)
    vdir = visual_diffs_dir()
    p = vdir / safe_name
    if not p.exists():
        abort(404)

    return send_from_directory(vdir, safe_name)


@web_bp.route("/projects/<int:project_id>/reports/<path:filename>", methods=["GET"])
def serve_report(project_id: int, filename: str):
    """
    Serve a report HTML file from instance/reports via URL.
    Setting mimetype helps iframe rendering.
    """
    gate = require_login()
    if gate:
        return gate

    safe_name = os.path.basename(filename)
    p = reports_dir() / safe_name
    if not p.exists():
        abort(404)

    return send_from_directory(reports_dir(), safe_name, mimetype="text/html")




# -----------------------
# Home / Projects
# -----------------------
@web_bp.route("/home")
def home():
    gate = require_login()
    if gate:
        return gate

    from datetime import datetime, timedelta
    from sqlalchemy import func

    projects = Project.query.order_by(Project.created_at.desc()).all()

    # KPI: runs this week
    week_ago = datetime.utcnow() - timedelta(days=7)
    runs_this_week = Run.query.filter(Run.created_at >= week_ago).count()

    # KPI: pass rate from last 20 completed runs
    recent_runs = Run.query.filter_by(status="completed").order_by(Run.created_at.desc()).limit(20).all()
    if recent_runs:
        total_checks = sum(r.total or 0 for r in recent_runs)
        total_passed = sum(r.passed or 0 for r in recent_runs)
        pass_rate = round((total_passed / total_checks * 100) if total_checks > 0 else 0)
    else:
        pass_rate = 0

    # KPI: open findings (errors in last run per project)
    open_findings = Run.query.filter_by(status="completed").order_by(Run.created_at.desc()).with_entities(
        func.sum(Run.errors)
    ).scalar() or 0
    open_findings = int(open_findings)

    # Per-project last run info
    last_runs = {}
    for p in projects:
        lr = Run.query.filter_by(project_id=p.id).order_by(Run.created_at.desc()).first()
        last_runs[p.id] = lr

    return render_template(
        "home.html",
        page_title="FAST | Project Dashboard",
        projects=projects,
        user=session.get("user"),
        active="home",
        runs_this_week=runs_this_week,
        pass_rate=pass_rate,
        open_findings=open_findings,
        last_runs=last_runs,
        now=datetime.utcnow(),
    )


@web_bp.route("/projects/create", methods=["GET", "POST"])
def create_project():
    gate = require_login()
    if gate:
        return gate

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()

        if not name:
            flash("Project name is required.", "error")
            return redirect(url_for("web.create_project"))

        p = Project(
            name=name,
            description=description,
            account=(request.form.get("account") or "").strip() or None,
            area=(request.form.get("area") or "").strip() or None,
            environment=(request.form.get("environment") or "").strip() or None,
        )
        db.session.add(p)
        db.session.commit()

        flash(f"Project '{name}' created successfully!", "success")
        return redirect(url_for("web.project_overview", project_id=p.id))

    return render_template(
        "create_project.html",
        page_title="FAST | Create Project",
        user=session.get("user"),
        active="home",
    )


@web_bp.route("/projects/<int:project_id>")
def project_overview(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)

    last_run = (
        Run.query.filter_by(project_id=project_id)
        .order_by(Run.created_at.desc())
        .first()
    )

    last_summary = {
        "errors": getattr(last_run, "errors", 0) if last_run else 0,
        "warnings": getattr(last_run, "warnings", 0) if last_run else 0,
        "passed": getattr(last_run, "passed", 0) if last_run else 0,
        "total": getattr(last_run, "total", 0) if last_run else 0,
        "run_id": last_run.id if last_run else None,
        "created_at": last_run.created_at if last_run else None,
        "triggered_by": getattr(last_run, "triggered_by", None) if last_run else None,
        "status": getattr(last_run, "status", None) if last_run else None,
    }

    recent_runs = (
        Run.query.filter_by(project_id=project_id)
        .order_by(Run.created_at.desc())
        .limit(5)
        .all()
    )

    form_count = Form.query.filter_by(project_id=project_id).count()
    test_case_count = TestCase.query.filter_by(project_id=project_id).count()
    total_run_count = Run.query.filter_by(project_id=project_id).count()

    all_runs = Run.query.filter_by(project_id=project_id).all()
    total_checks = sum((r.passed or 0) + (r.errors or 0) for r in all_runs)
    total_passed = sum(r.passed or 0 for r in all_runs)
    pass_rate = round((total_passed / total_checks) * 100) if total_checks > 0 else 0

    return render_template(
        "project_overview.html",
        page_title=f"FAST | {project.name}",
        project=project,
        last_run=last_run,
        last_summary=last_summary,
        recent_runs=recent_runs,
        form_count=form_count,
        test_case_count=test_case_count,
        total_run_count=total_run_count,
        pass_rate=pass_rate,
        user=session.get("user"),
    )

# This endpoint can be polled by the frontend to get real-time status updates for a run.
@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/status", methods=["GET"])
def run_status(project_id, run_id):
    rr = RunResult.query.filter_by(project_id=project_id, run_id=run_id).first_or_404()

    return jsonify({
        "run_id": run_id,
        "status": rr.status,  # queued / running / processing / completed / failed
        "progress_percent": rr.progress_percent or 0,
        "redirect_url": url_for("web.project_results", project_id=project_id, run_id=run_id)
    })

# -----------------------
# Forms
# -----------------------
@web_bp.route("/projects/<int:project_id>/forms", methods=["GET"])
def project_forms(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    forms = Form.query.filter_by(project_id=project_id).order_by(Form.uploaded_at.desc()).all()

    return render_template(
        "forms.html",
        page_title="FAST | Forms",
        project=project,
        forms=forms,
        max_allowed=MAX_FORMS_PER_PROJECT,
        user=session.get("user"),
        active="forms",
    )


@web_bp.route("/projects/<int:project_id>/forms/upload", methods=["POST"])
def upload_project_forms(project_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)

    current_count = Form.query.filter_by(project_id=project_id).count()
    if current_count >= MAX_FORMS_PER_PROJECT:
        flash(f"Maximum {MAX_FORMS_PER_PROJECT} forms allowed per project.", "error")
        return redirect(url_for("web.project_forms", project_id=project_id))

    if "files" not in request.files:
        flash("No files found in request.", "error")
        return redirect(url_for("web.project_forms", project_id=project_id))

    files = request.files.getlist("files")
    if not files or all((f.filename or "") == "" for f in files):
        flash("Please select at least one PDF to upload.", "error")
        return redirect(url_for("web.project_forms", project_id=project_id))

    store_dir = project_forms_dir(project_id)
    uploaded_count = 0
    skipped_count = 0

    for f in files:
        if not f or not f.filename:
            continue

        if not allowed_file(f.filename):
            skipped_count += 1
            continue

        if current_count + uploaded_count >= MAX_FORMS_PER_PROJECT:
            skipped_count += 1
            continue

        filename = secure_filename(f.filename)
        file_path = os.path.join(store_dir, filename)

        base = Path(filename).stem
        ext = Path(filename).suffix
        counter = 1
        while os.path.exists(file_path):
            new_name = f"{base}_{counter}{ext}"
            file_path = os.path.join(store_dir, new_name)
            filename = new_name
            counter += 1

        f.save(file_path)

        p = Path(file_path)
        form = Form(
            project_id=project_id,
            name=p.stem,
            file_path=str(p),
            original_filename=f.filename,
            stored_filename=filename,
            size_bytes=p.stat().st_size if p.exists() else None,
            version="v1",
        )

        db.session.add(form)
        uploaded_count += 1

    if uploaded_count > 0:
        try:
            db.session.commit()
            flash(
                f"Uploaded {uploaded_count} form(s)."
                + (f" Skipped {skipped_count}." if skipped_count else ""),
                "success",
            )
        except Exception as e:
            db.session.rollback()
            flash(f"Upload failed while saving to DB: {str(e)}", "error")
    else:
        flash("No valid PDFs were uploaded.", "error")

    return redirect(url_for("web.project_forms", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/delete", methods=["POST"])
def delete_form(project_id: int, form_id: int):
    return delete_project_form(project_id, form_id)


@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/delete_project_form", methods=["POST"])
def delete_project_form(project_id: int, form_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)
    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()

    store_dir = project_forms_dir(project_id)
    if form.stored_filename:
        file_path = os.path.join(store_dir, form.stored_filename)
        if os.path.exists(file_path):
            os.remove(file_path)

    db.session.delete(form)
    db.session.commit()
    flash("Form deleted.", "success")
    return redirect(url_for("web.project_forms", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/view", methods=["GET"])
def view_form_file(project_id: int, form_id: int):
    """
    Serves the uploaded PDF through Flask, so the report can embed it.
    """
    gate = require_login()
    if gate:
        return gate

    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()
    if not form.file_path or not os.path.exists(form.file_path):
        abort(404)

    return send_file(form.file_path, mimetype="application/pdf", as_attachment=False)


# -----------------------
# Test Cases
# -----------------------
@web_bp.route("/projects/<int:project_id>/testcases", methods=["GET"])
def project_testcases(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    test_cases = TestCase.query.filter_by(project_id=project_id).order_by(TestCase.created_at.desc()).all()
    forms = Form.query.filter_by(project_id=project_id).order_by(Form.uploaded_at.desc()).all()

    return render_template(
        "test_cases.html",
        page_title="FAST | Test Cases",
        project=project,
        test_cases=test_cases,
        forms=forms,
        active="testcases",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/testcases/new", methods=["GET"])
def new_testcase(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    forms = Form.query.filter_by(project_id=project_id).order_by(Form.uploaded_at.desc()).all()

    return render_template(
        "test_case_new.html",
        page_title="FAST | New Test Case",
        project=project,
        forms=forms,
        active="testcases",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/testcases/create", methods=["POST"])
def create_testcase(project_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)

    name = (request.form.get("name") or "").strip()
    mode = (request.form.get("mode") or "").strip().lower()
    form_id = request.form.get("form_id") or None
    benchmark_form_id = request.form.get("benchmark_form_id") or None
    prompt_text = (request.form.get("prompt_text") or "").strip()

    if not name:
        flash("Test case name is required.", "error")
        return redirect(url_for("web.new_testcase", project_id=project_id))

    if mode not in {"basic", "specific", "benchmark"}:
        flash("Invalid mode selected.", "error")
        return redirect(url_for("web.new_testcase", project_id=project_id))

    form_id = int(form_id) if form_id else None
    benchmark_form_id = int(benchmark_form_id) if benchmark_form_id else None

    if mode == "benchmark" and not benchmark_form_id:
        flash("Benchmark (golden copy) is required for Benchmark mode.", "error")
        return redirect(url_for("web.new_testcase", project_id=project_id))

    # NOTE: in CLI, specific can fall back to basic if no rules.
    # So we do NOT hard-reject missing prompt_text for 'specific'.
    if mode == "benchmark" and not prompt_text:
        flash("Prompt/Test steps are required for Benchmark.", "error")
        return redirect(url_for("web.new_testcase", project_id=project_id))

    tc = TestCase(
        project_id=project_id,
        name=name,
        mode=mode,
        form_id=form_id,
        benchmark_form_id=benchmark_form_id,
        prompt_text=prompt_text if prompt_text else None,
    )
    db.session.add(tc)
    db.session.commit()

    flash("Test case created.", "success")
    return redirect(url_for("web.project_testcases", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/testcases/<int:testcase_id>/delete", methods=["POST"])
def delete_testcase(project_id: int, testcase_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)
    tc = TestCase.query.filter_by(id=testcase_id, project_id=project_id).first_or_404()

    db.session.delete(tc)
    db.session.commit()
    flash("Test case deleted.", "success")
    return redirect(url_for("web.project_testcases", project_id=project_id))


# -----------------------
# Execute
# -----------------------
@web_bp.route("/projects/<int:project_id>/execute", methods=["GET"])
def project_execute(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    test_cases = TestCase.query.filter_by(project_id=project_id).order_by(TestCase.created_at.desc()).all()

    return render_template(
        "execute.html",
        page_title="FAST | Execute",
        project=project,
        test_cases=test_cases,
        active="execute",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/execute/run", methods=["POST"])
def execute_run(project_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)

    tc_ids = request.form.getlist("testcase_ids")
    if not tc_ids:
        flash("Select at least one test case to execute.", "error")
        return redirect(url_for("web.project_execute", project_id=project_id))

    tc_ids = [int(x) for x in tc_ids]
    test_cases = TestCase.query.filter(TestCase.project_id == project_id, TestCase.id.in_(tc_ids)).all()

    run = Run(project_id=project_id)
    if hasattr(run, "triggered_by"):
        run.triggered_by = session.get("user")
    if hasattr(run, "total"):
        run.total = len(test_cases)

    db.session.add(run)
    db.session.commit()  # get run.id

    total_errors = total_warnings = total_passed = 0

    for tc in test_cases:
        rr = RunResult(run_id=run.id, test_case_id=tc.id)

        if hasattr(rr, "project_id"):
            rr.project_id = project_id
        if hasattr(rr, "form_id"):
            rr.form_id = tc.form_id
        if hasattr(rr, "mode"):
            rr.mode = tc.mode

        rr.status = "running"
        db.session.add(rr)
        db.session.commit()  # get rr.id

        try:
            out = run_testcase(project_id=project_id, tc=tc, run_id=run.id, rr_id=rr.id)

            rr.result_json = json.dumps(out.get("result_json") or {}, ensure_ascii=False, indent=2)
            rr.summary_text = out.get("summary_text") or ""

            result_obj = out.get("result_json") or {}
            if isinstance(result_obj, dict) and result_obj.get("error"):
                rr.status = "failed"
            else:
                rr.status = "completed"

            rr.errors = int(out.get("errors") or 0) if hasattr(rr, "errors") else 0
            rr.warnings = int(out.get("warnings") or 0) if hasattr(rr, "warnings") else 0
            rr.passed = int(out.get("passed") or 0) if hasattr(rr, "passed") else 0

            write_fn = out.get("write_report_fn")
            if callable(write_fn):
                report_filename = write_fn(
                    project_id=project_id,
                    run_id=run.id,
                    rr_id=rr.id,
                    tc=tc,
                    result_json=out.get("result_json") or {},
                    llm_summary=out.get("summary_text") or "",
                    main_form=out.get("main_form"),
                    bench_form=out.get("bench_form"),
                )
                rr.report_html_path = report_filename  # filename only

            total_errors += rr.errors if hasattr(rr, "errors") else 0
            total_warnings += rr.warnings if hasattr(rr, "warnings") else 0
            total_passed += rr.passed if hasattr(rr, "passed") else 0

        except Exception as e:
            rr.status = "failed"
            if hasattr(rr, "error_message"):
                rr.error_message = str(e)
            rr.summary_text = f"Execution failed: {str(e)}"
            rr.result_json = json.dumps({"error": str(e)}, indent=2)

            if hasattr(rr, "errors"):
                rr.errors = 1
            if hasattr(rr, "warnings"):
                rr.warnings = 0
            if hasattr(rr, "passed"):
                rr.passed = 0

            total_errors += 1

        db.session.add(rr)
        db.session.commit()

    if hasattr(run, "errors"):
        run.errors = total_errors
    if hasattr(run, "warnings"):
        run.warnings = total_warnings
    if hasattr(run, "passed"):
        run.passed = total_passed
    if hasattr(run, "status"):
        run.status = "completed"

    db.session.commit()

    flash(f"Run #{run.id} completed.", "success")
    return redirect(url_for("web.project_results", project_id=project_id, run_id=run.id))


# -----------------------
# Results
# -----------------------
@web_bp.route("/projects/<int:project_id>/results", methods=["GET"])
def project_results(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    run_id = request.args.get("run_id", type=int)

    runs = Run.query.filter_by(project_id=project_id).order_by(Run.created_at.desc()).limit(20).all()

    # Default to the most recent run
    if not run_id and runs:
        run_id = runs[0].id

    selected_run = None
    results = []
    if run_id:
        selected_run = Run.query.filter_by(id=run_id, project_id=project_id).first()
        if selected_run:
            results = RunResult.query.filter_by(run_id=run_id).order_by(RunResult.id.asc()).all()

    # Build test case name lookup
    tc_ids = [rr.test_case_id for rr in results if rr.test_case_id]
    tc_map = {}
    if tc_ids:
        tcs = TestCase.query.filter(TestCase.id.in_(tc_ids)).all()
        tc_map = {tc.id: tc for tc in tcs}

    return render_template(
        "results.html",
        page_title="FAST | Results",
        project=project,
        runs=runs,
        selected_run=selected_run,
        results=results,
        tc_map=tc_map,
        active="results",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>", methods=["GET"])
def result_detail(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    rr = RunResult.query.get_or_404(result_id)
    tc = TestCase.query.filter_by(id=rr.test_case_id, project_id=project_id).first()

    report_url = None
    if getattr(rr, "report_html_path", None):
        report_url = url_for("web.serve_report", project_id=project_id, filename=rr.report_html_path)

    return render_template(
        "result_detail.html",
        page_title="FAST | Result Detail",
        project=project,
        rr=rr,
        tc=tc,
        report_url=report_url,
        active="results",
        user=session.get("user"),
    )


# -----------------------
# Marketing / Contact
# -----------------------
@web_bp.route("/contact")
def contact():
    gate = require_login()
    if gate:
        return gate

    return render_template(
        "contact.html",
        page_title="FAST | Contact Us",
        user=session.get("user"),
        active="contact",
    )


# ===========================================================================
# NEW FEATURE ROUTES
# ===========================================================================

# -----------------------
# Trend Dashboard
# -----------------------
@web_bp.route("/projects/<int:project_id>/trends")
def project_trends(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    runs = Run.query.filter_by(project_id=project_id).order_by(Run.created_at.asc()).limit(50).all()

    trend_data = [{
        "run_id": r.id,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
        "errors": r.errors or 0,
        "warnings": r.warnings or 0,
        "passed": r.passed or 0,
        "total": r.total or 0,
        "status": r.status,
    } for r in runs]

    return render_template(
        "trends.html",
        page_title=f"FAST | Trends — {project.name}",
        project=project,
        trend_data_json=json.dumps(trend_data),
        runs=runs,
        active="trends",
        user=session.get("user"),
    )


# -----------------------
# Finding Review Workflow
# -----------------------
@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/reviews")
def result_reviews(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    rr = RunResult.query.get_or_404(result_id)
    tc = TestCase.query.filter_by(id=rr.test_case_id, project_id=project_id).first()

    # Parse findings into a flat list with index
    findings = []
    result_obj = {}
    if rr.result_json:
        try:
            result_obj = json.loads(rr.result_json)
        except Exception:
            pass

    categories = [
        "spelling_errors", "format_issues", "value_mismatches",
        "missing_content", "extra_content", "layout_anomalies",
        "compliance_issues", "visual_mismatches",
    ]
    idx = 0
    for cat in categories:
        for item in result_obj.get(cat, []):
            review = FindingReview.query.filter_by(
                run_result_id=result_id, finding_index=idx
            ).first()
            findings.append({
                "index": idx,
                "category": cat,
                "item": item,
                "review": review,
            })
            idx += 1

    # Available reviewers
    reviewers = User.query.filter(User.role.in_(["admin", "reviewer"]), User.is_active == True).all()
    compliance_standards = ComplianceStandard.query.all()

    return render_template(
        "finding_reviews.html",
        page_title=f"FAST | Review Findings",
        project=project,
        rr=rr,
        tc=tc,
        findings=findings,
        reviewers=reviewers,
        compliance_standards=compliance_standards,
        active="results",
        user=session.get("user"),
        user_role=session.get("role", "viewer"),
    )


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/reviews/assign", methods=["POST"])
def assign_finding(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate
    rr_check = require_role("reviewer")
    if rr_check:
        return rr_check

    RunResult.query.get_or_404(result_id)

    finding_index = request.form.get("finding_index", type=int)
    finding_category = request.form.get("finding_category", "")
    finding_description = request.form.get("finding_description", "")
    assigned_to = request.form.get("assigned_to", "")

    review = FindingReview.query.filter_by(
        run_result_id=result_id, finding_index=finding_index
    ).first()

    from datetime import datetime
    if not review:
        review = FindingReview(
            run_result_id=result_id,
            project_id=project_id,
            finding_index=finding_index,
            finding_category=finding_category,
            finding_description=finding_description,
            status="in_review",
        )
        db.session.add(review)
    else:
        review.status = "in_review"

    review.assigned_to = assigned_to
    review.assigned_by = session.get("user")
    review.assigned_at = datetime.utcnow()
    db.session.commit()

    log_action("finding.assigned", resource_type="finding_review", resource_id=review.id,
               project_id=project_id, detail={"assigned_to": assigned_to})
    flash(f"Finding assigned to {assigned_to}.", "success")
    return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/reviews/resolve", methods=["POST"])
def resolve_finding(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate

    RunResult.query.get_or_404(result_id)

    finding_index = request.form.get("finding_index", type=int)
    finding_category = request.form.get("finding_category", "")
    finding_description = request.form.get("finding_description", "")
    new_status = request.form.get("status", "resolved")  # resolved | false_positive
    resolution_note = request.form.get("resolution_note", "")

    if new_status not in ("resolved", "false_positive", "open"):
        new_status = "resolved"

    from datetime import datetime
    review = FindingReview.query.filter_by(
        run_result_id=result_id, finding_index=finding_index
    ).first()
    if not review:
        review = FindingReview(
            run_result_id=result_id,
            project_id=project_id,
            finding_index=finding_index,
            finding_category=finding_category,
            finding_description=finding_description,
        )
        db.session.add(review)

    review.status = new_status
    review.resolved_by = session.get("user")
    review.resolved_at = datetime.utcnow()
    review.resolution_note = resolution_note
    db.session.commit()

    # Auto-learn from false positives
    if new_status == "false_positive":
        from app.services.auto_learning import learn_false_positive
        learn_false_positive(review.id, project_id, created_by=session.get("user"))
        flash("Finding marked as false positive — pattern learned for future runs.", "info")
    else:
        flash(f"Finding marked as {new_status}.", "success")

    log_action(f"finding.{new_status}", resource_type="finding_review", resource_id=review.id,
               project_id=project_id)
    return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/reviews/comment", methods=["POST"])
def add_finding_comment(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate

    finding_index = request.form.get("finding_index", type=int)
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))

    review = FindingReview.query.filter_by(
        run_result_id=result_id, finding_index=finding_index
    ).first()
    if not review:
        flash("Finding review not found.", "error")
        return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))

    comment = FindingComment(
        finding_review_id=review.id,
        author=session.get("user"),
        body=body,
    )
    db.session.add(comment)
    db.session.commit()
    flash("Comment added.", "success")
    return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))


# -----------------------
# Approval Gates
# -----------------------
@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/gate")
def run_approval_gate(project_id: int, run_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    run = Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()
    approval = ApprovalGate.query.filter_by(run_id=run_id).first()

    # Auto-create if none
    if not approval:
        approval = ApprovalGate(project_id=project_id, run_id=run_id)
        db.session.add(approval)
        db.session.commit()

    return render_template(
        "approval_gate.html",
        page_title="FAST | Approval Gate",
        project=project,
        run=run,
        approval=approval,
        active="results",
        user=session.get("user"),
        user_role=session.get("role", "viewer"),
    )


@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/gate/review", methods=["POST"])
def review_approval_gate(project_id: int, run_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    run = Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()
    approval = ApprovalGate.query.filter_by(run_id=run_id).first()
    if not approval:
        approval = ApprovalGate(project_id=project_id, run_id=run_id)
        db.session.add(approval)

    decision = request.form.get("decision")  # approved | rejected
    review_note = (request.form.get("review_note") or "").strip()

    if decision not in ("approved", "rejected"):
        flash("Invalid decision.", "error")
        return redirect(url_for("web.run_approval_gate", project_id=project_id, run_id=run_id))

    from datetime import datetime
    approval.status = decision
    approval.reviewed_by = session.get("user")
    approval.reviewed_at = datetime.utcnow()
    approval.review_note = review_note
    db.session.commit()

    from app.services.webhook_service import fire_event
    fire_event(f"gate.{decision}", {
        "run_id": run_id, "decision": decision, "reviewed_by": session.get("user")
    }, project_id)

    log_action(f"gate.{decision}", resource_type="approval_gate", resource_id=approval.id,
               project_id=project_id, run_id=run_id)
    flash(f"Run #{run_id} {decision}.", "success" if decision == "approved" else "error")
    return redirect(url_for("web.run_approval_gate", project_id=project_id, run_id=run_id))


# -----------------------
# Evidence Bundle Download
# -----------------------
@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/bundle")
def download_evidence_bundle(project_id: int, run_id: int):
    gate = require_login()
    if gate:
        return gate

    Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()

    try:
        from app.services.evidence_bundle import build_evidence_bundle
        bundle_bytes = build_evidence_bundle(run_id=run_id, project_id=project_id)
        log_action("bundle.downloaded", resource_type="run", resource_id=run_id, project_id=project_id)
        return send_file(
            io.BytesIO(bundle_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"evidence_bundle_run_{run_id}.zip",
        )
    except Exception as e:
        flash(f"Bundle generation failed: {e}", "error")
        return redirect(url_for("web.project_results", project_id=project_id, run_id=run_id))


# -----------------------
# Annotated PDF Download
# -----------------------
@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/annotated_pdf")
def download_annotated_pdf(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate

    rr = RunResult.query.get_or_404(result_id)
    tc = TestCase.query.filter_by(id=rr.test_case_id, project_id=project_id).first_or_404()
    form = Form.query.get(tc.form_id) if tc.form_id else None

    if not form or not form.file_path or not os.path.exists(form.file_path):
        flash("Original PDF not found.", "error")
        return redirect(url_for("web.result_detail", project_id=project_id, result_id=result_id))

    with open(form.file_path, "rb") as f:
        pdf_bytes = f.read()

    result_obj = {}
    if rr.result_json:
        try:
            result_obj = json.loads(rr.result_json)
        except Exception:
            pass

    try:
        from engine.pdf_annotator import annotate_pdf
        annotated = annotate_pdf(pdf_bytes, result_obj)
        log_action("pdf.annotated_download", resource_type="run_result", resource_id=result_id,
                   project_id=project_id)
        return send_file(
            io.BytesIO(annotated),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"annotated_result_{result_id}.pdf",
        )
    except Exception as e:
        flash(f"Annotated PDF generation failed: {e}", "error")
        return redirect(url_for("web.result_detail", project_id=project_id, result_id=result_id))


# -----------------------
# Natural Language Test Case Builder
# -----------------------
@web_bp.route("/projects/<int:project_id>/testcases/nlbuild", methods=["GET", "POST"])
def nl_testcase_builder(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    forms = Form.query.filter_by(project_id=project_id).order_by(Form.uploaded_at.desc()).all()

    result = None
    description = ""
    if request.method == "POST":
        description = (request.form.get("description") or "").strip()
        provider = request.form.get("provider") or None

        if not description:
            flash("Please enter a description.", "error")
        else:
            from app.services.nl_testcase_builder import build_testcase_from_nl
            result = build_testcase_from_nl(description, provider=provider)
            if result.get("error"):
                flash(f"Builder error: {result['error']}", "error")
                result = None

    from engine.llm_client import get_available_providers
    available_providers = get_available_providers()

    return render_template(
        "nl_testcase_builder.html",
        page_title="FAST | AI Test Case Builder",
        project=project,
        forms=forms,
        result=result,
        description=description,
        available_providers=available_providers,
        active="testcases",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/testcases/nlbuild/save", methods=["POST"])
def save_nl_testcase(project_id: int):
    gate = require_login()
    if gate:
        return gate

    Project.query.get_or_404(project_id)

    name = (request.form.get("name") or "").strip()
    mode = (request.form.get("mode") or "specific").strip()
    prompt_text = (request.form.get("prompt_text") or "").strip()
    form_id = request.form.get("form_id") or None
    benchmark_form_id = request.form.get("benchmark_form_id") or None

    if not name:
        flash("Test case name is required.", "error")
        return redirect(url_for("web.nl_testcase_builder", project_id=project_id))

    tc = TestCase(
        project_id=project_id,
        name=name,
        mode=mode if mode in ("basic", "specific", "benchmark") else "specific",
        form_id=int(form_id) if form_id else None,
        benchmark_form_id=int(benchmark_form_id) if benchmark_form_id else None,
        prompt_text=prompt_text or None,
    )
    db.session.add(tc)
    db.session.commit()
    log_action("testcase.created_nl", resource_type="test_case", resource_id=tc.id, project_id=project_id)
    flash("Test case created from AI builder.", "success")
    return redirect(url_for("web.project_testcases", project_id=project_id))


# -----------------------
# Webhooks
# -----------------------
@web_bp.route("/projects/<int:project_id>/webhooks")
def project_webhooks(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    webhooks = WebhookConfig.query.filter_by(project_id=project_id).all()
    return render_template(
        "webhooks.html",
        page_title="FAST | Webhooks",
        project=project,
        webhooks=webhooks,
        active="settings",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/webhooks/create", methods=["POST"])
def create_webhook(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    Project.query.get_or_404(project_id)
    url_val = (request.form.get("url") or "").strip()
    if not url_val:
        flash("URL is required.", "error")
        return redirect(url_for("web.project_webhooks", project_id=project_id))

    wh = WebhookConfig(
        project_id=project_id,
        name=request.form.get("name") or "Webhook",
        url=url_val,
        secret=request.form.get("secret") or None,
        events=request.form.get("events") or "run.completed",
        created_by=session.get("user"),
    )
    db.session.add(wh)
    db.session.commit()
    log_action("webhook.created", resource_type="webhook_config", resource_id=wh.id, project_id=project_id)
    flash("Webhook created.", "success")
    return redirect(url_for("web.project_webhooks", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/webhooks/<int:webhook_id>/delete", methods=["POST"])
def delete_webhook(project_id: int, webhook_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    wh = WebhookConfig.query.filter_by(id=webhook_id, project_id=project_id).first_or_404()
    db.session.delete(wh)
    db.session.commit()
    flash("Webhook deleted.", "success")
    return redirect(url_for("web.project_webhooks", project_id=project_id))


# -----------------------
# Scheduled Runs
# -----------------------
@web_bp.route("/projects/<int:project_id>/schedules")
def project_schedules(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    schedules = ScheduledRun.query.filter_by(project_id=project_id).all()
    test_cases = TestCase.query.filter_by(project_id=project_id).all()
    return render_template(
        "schedules.html",
        page_title="FAST | Scheduled Runs",
        project=project,
        schedules=schedules,
        test_cases=test_cases,
        active="settings",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/schedules/create", methods=["POST"])
def create_schedule(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    Project.query.get_or_404(project_id)

    name = (request.form.get("name") or "").strip()
    cron_expr = (request.form.get("cron_expression") or "").strip()
    tc_ids = request.form.getlist("testcase_ids")

    if not name or not cron_expr or not tc_ids:
        flash("Name, cron expression, and at least one test case are required.", "error")
        return redirect(url_for("web.project_schedules", project_id=project_id))

    from app.services.scheduler import compute_next_run
    next_run = compute_next_run(cron_expr)

    sched = ScheduledRun(
        project_id=project_id,
        name=name,
        cron_expression=cron_expr,
        testcase_ids=",".join(tc_ids),
        triggered_by=session.get("user"),
        next_run_at=next_run,
    )
    db.session.add(sched)
    db.session.commit()
    log_action("schedule.created", resource_type="scheduled_run", resource_id=sched.id, project_id=project_id)
    flash(f"Schedule '{name}' created. Next run: {next_run}.", "success")
    return redirect(url_for("web.project_schedules", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/schedules/<int:sched_id>/toggle", methods=["POST"])
def toggle_schedule(project_id: int, sched_id: int):
    gate = require_login()
    if gate:
        return gate

    sched = ScheduledRun.query.filter_by(id=sched_id, project_id=project_id).first_or_404()
    sched.is_active = not sched.is_active
    db.session.commit()
    flash(f"Schedule {'enabled' if sched.is_active else 'disabled'}.", "success")
    return redirect(url_for("web.project_schedules", project_id=project_id))


@web_bp.route("/projects/<int:project_id>/schedules/<int:sched_id>/delete", methods=["POST"])
def delete_schedule(project_id: int, sched_id: int):
    gate = require_login()
    if gate:
        return gate

    sched = ScheduledRun.query.filter_by(id=sched_id, project_id=project_id).first_or_404()
    db.session.delete(sched)
    db.session.commit()
    flash("Schedule deleted.", "success")
    return redirect(url_for("web.project_schedules", project_id=project_id))


# -----------------------
# Field Inventory
# -----------------------
@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/inventory")
def form_field_inventory(project_id: int, form_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()
    fields = FieldInventory.query.filter_by(form_id=form_id).order_by(FieldInventory.page_number, FieldInventory.field_name).all()

    return render_template(
        "field_inventory.html",
        page_title=f"FAST | Field Inventory — {form.name}",
        project=project,
        form=form,
        fields=fields,
        active="forms",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/scan_inventory", methods=["POST"])
def scan_field_inventory(project_id: int, form_id: int):
    gate = require_login()
    if gate:
        return gate

    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()

    if not form.file_path or not os.path.exists(form.file_path):
        flash("PDF file not found on disk.", "error")
        return redirect(url_for("web.project_forms", project_id=project_id))

    try:
        from engine.extractor import extract_all
        from engine.accessibility_checker import build_field_inventory

        with open(form.file_path, "rb") as f:
            pdf_bytes = f.read()

        extraction = extract_all(pdf_bytes)
        records = build_field_inventory(extraction, project_id=project_id, form_id=form_id)

        # Delete old inventory for this form
        FieldInventory.query.filter_by(form_id=form_id).delete()

        for rec in records:
            fi = FieldInventory(**rec)
            db.session.add(fi)

        db.session.commit()
        flash(f"Scanned {len(records)} field(s) from {form.name}.", "success")
    except Exception as e:
        flash(f"Scan failed: {e}", "error")

    return redirect(url_for("web.form_field_inventory", project_id=project_id, form_id=form_id))


# -----------------------
# Accessibility Check
# -----------------------
@web_bp.route("/projects/<int:project_id>/forms/<int:form_id>/accessibility")
def form_accessibility(project_id: int, form_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()

    issues = []
    if form.file_path and os.path.exists(form.file_path):
        try:
            from engine.extractor import extract_all
            from engine.accessibility_checker import check_accessibility
            with open(form.file_path, "rb") as f:
                pdf_bytes = f.read()
            extraction = extract_all(pdf_bytes)
            issues = check_accessibility(extraction)
        except Exception as e:
            flash(f"Accessibility check failed: {e}", "error")

    return render_template(
        "accessibility.html",
        page_title=f"FAST | Accessibility — {form.name}",
        project=project,
        form=form,
        issues=issues,
        active="forms",
        user=session.get("user"),
    )


# -----------------------
# Compliance Standards
# -----------------------
@web_bp.route("/compliance")
def compliance_standards(project_id: int = None):
    gate = require_login()
    if gate:
        return gate

    standards = ComplianceStandard.query.all()
    return render_template(
        "compliance_standards.html",
        page_title="FAST | Compliance Standards",
        standards=standards,
        user=session.get("user"),
        active="compliance",
    )


# -----------------------
# False Positive Management
# -----------------------
@web_bp.route("/projects/<int:project_id>/false-positives")
def project_false_positives(project_id: int):
    gate = require_login()
    if gate:
        return gate

    project = Project.query.get_or_404(project_id)
    fps = FalsePositive.query.filter_by(project_id=project_id).order_by(FalsePositive.created_at.desc()).all()
    return render_template(
        "false_positives.html",
        page_title="FAST | Learned False Positives",
        project=project,
        false_positives=fps,
        active="settings",
        user=session.get("user"),
    )


@web_bp.route("/projects/<int:project_id>/false-positives/<int:fp_id>/toggle", methods=["POST"])
def toggle_false_positive(project_id: int, fp_id: int):
    gate = require_login()
    if gate:
        return gate

    fp = FalsePositive.query.filter_by(id=fp_id, project_id=project_id).first_or_404()
    fp.is_active = not fp.is_active
    db.session.commit()
    flash(f"Pattern {'enabled' if fp.is_active else 'disabled'}.", "success")
    return redirect(url_for("web.project_false_positives", project_id=project_id))


# -----------------------
# Admin Panel
# -----------------------
@web_bp.route("/admin")
def admin_panel():
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    users = User.query.order_by(User.created_at.desc()).all()
    audit_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()
    api_keys = ApiKey.query.filter_by(is_active=True).order_by(ApiKey.created_at.desc()).all()
    total_projects = Project.query.count()
    total_runs = Run.query.count()
    total_forms = Form.query.count()

    return render_template(
        "admin_panel.html",
        page_title="FAST | Admin",
        users=users,
        audit_logs=audit_logs,
        api_keys=api_keys,
        total_projects=total_projects,
        total_runs=total_runs,
        total_forms=total_forms,
        active="admin",
        user=session.get("user"),
        user_role=session.get("role", "viewer"),
    )


@web_bp.route("/admin/users/create", methods=["POST"])
def admin_create_user():
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    display_name = (request.form.get("display_name") or "").strip()
    role = (request.form.get("role") or "viewer").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("web.admin_panel"))

    if User.query.filter_by(username=username).first():
        flash(f"Username '{username}' already exists.", "error")
        return redirect(url_for("web.admin_panel"))

    user_obj = User(
        username=username,
        email=email or None,
        display_name=display_name or username,
        role=role if role in ("admin", "reviewer", "viewer") else "viewer",
        is_active=True,
    )
    user_obj.set_password(password)
    db.session.add(user_obj)
    db.session.commit()
    log_action("admin.user_created", resource_type="user", resource_id=user_obj.id,
               detail={"username": username, "role": role})
    flash(f"User '{username}' created with role '{role}'.", "success")
    return redirect(url_for("web.admin_panel"))


@web_bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
def admin_toggle_user(user_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    user_obj = User.query.get_or_404(user_id)
    if user_obj.username == session.get("user"):
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("web.admin_panel"))

    user_obj.is_active = not user_obj.is_active
    db.session.commit()
    flash(f"User '{user_obj.username}' {'activated' if user_obj.is_active else 'deactivated'}.", "success")
    return redirect(url_for("web.admin_panel"))


@web_bp.route("/admin/api-keys/create", methods=["POST"])
def admin_create_api_key():
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("web.admin_panel"))

    raw_key, prefix, key_hash = ApiKey.generate()
    ak = ApiKey(
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
        scopes="validate:read,projects:read,runs:read",
        created_by=session.get("user"),
        is_active=True,
    )
    db.session.add(ak)
    db.session.commit()
    log_action("admin.api_key_created", resource_type="api_key", resource_id=ak.id)

    # Show key once via flash
    flash(f"API Key created. Save this key — it will not be shown again: {raw_key}", "info")
    return redirect(url_for("web.admin_panel"))


@web_bp.route("/admin/api-keys/<int:key_id>/revoke", methods=["POST"])
def admin_revoke_api_key(key_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    ak = ApiKey.query.get_or_404(key_id)
    ak.is_active = False
    db.session.commit()
    flash(f"API key '{ak.name}' revoked.", "success")
    return redirect(url_for("web.admin_panel"))


# -----------------------
# Audit Log (read-only)
# -----------------------
@web_bp.route("/projects/<int:project_id>/audit-log")
def project_audit_log(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    project = Project.query.get_or_404(project_id)
    logs = AuditLog.query.filter_by(project_id=project_id).order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template(
        "audit_log.html",
        page_title=f"FAST | Audit Log — {project.name}",
        project=project,
        logs=logs,
        active="settings",
        user=session.get("user"),
    )
