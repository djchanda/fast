# app/routes/web.py
import io
import logging
import os
import json
from pathlib import Path

logger = logging.getLogger(__name__)
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
from app.models.jira_config import JiraConfig
from app.models.project_member import ProjectMember
from app.services.runner import run_testcase
from app.services.audit import log_action
from app.services import jira_client


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


@web_bp.before_request
def _enforce_project_access():
    """Block non-admin users from accessing projects they are not assigned to."""
    role = session.get("role")
    if role == "admin" or role is None:
        return None
    if not request.view_args:
        return None
    project_id = request.view_args.get("project_id")
    if project_id is None:
        return None
    if not is_logged_in():
        return None
    assigned = ProjectMember.query.filter_by(
        project_id=project_id,
        user_id=session.get("user_id"),
    ).first()
    if not assigned:
        flash("Access denied: you are not assigned to this project.", "error")
        return redirect(url_for("web.home"))


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

    if session.get("role") != "admin":
        member_pids = [
            m.project_id for m in
            ProjectMember.query.filter_by(user_id=session.get("user_id")).all()
        ]
        projects = Project.query.filter(Project.id.in_(member_pids)).order_by(Project.created_at.desc()).all()
    else:
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
    gate = require_login() or require_role("admin")
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

    # Per-test-case summary — batched to avoid N+1 queries
    from sqlalchemy import func as _func
    test_cases = TestCase.query.filter_by(project_id=project_id).order_by(TestCase.id.asc()).all()

    # 1) Latest RunResult id per test case
    _latest_subq = (
        db.session.query(_func.max(RunResult.id).label("max_id"))
        .filter(RunResult.project_id == project_id)
        .group_by(RunResult.test_case_id)
        .subquery()
    )
    latest_rrs = RunResult.query.filter(RunResult.id.in_(_latest_subq)).all()
    latest_rr_by_tc = {rr.test_case_id: rr for rr in latest_rrs}
    latest_rr_ids = [rr.id for rr in latest_rrs]

    # 2) FindingReview status counts for all latest RunResults
    _review_rows = (
        db.session.query(
            FindingReview.run_result_id,
            FindingReview.status,
            _func.count(FindingReview.id).label("cnt"),
        )
        .filter(FindingReview.run_result_id.in_(latest_rr_ids))
        .group_by(FindingReview.run_result_id, FindingReview.status)
        .all()
    ) if latest_rr_ids else []
    review_stats: dict = {}
    for row in _review_rows:
        review_stats.setdefault(row.run_result_id, {})[row.status] = row.cnt

    # 3) All RunResult ids for this project (for Jira key lookup across reruns)
    all_project_rrs = RunResult.query.filter_by(project_id=project_id).with_entities(
        RunResult.id, RunResult.test_case_id
    ).all()
    rr_to_tc = {r.id: r.test_case_id for r in all_project_rrs}
    all_project_rr_ids = list(rr_to_tc.keys())

    # 4) Jira keys grouped by test case
    jira_by_tc: dict = {}
    if all_project_rr_ids:
        _jira_rows = FindingReview.query.filter(
            FindingReview.run_result_id.in_(all_project_rr_ids),
            FindingReview.jira_issue_key.isnot(None),
        ).with_entities(FindingReview.run_result_id, FindingReview.jira_issue_key).all()
        for row in _jira_rows:
            tc_id = rr_to_tc.get(row.run_result_id)
            if tc_id and row.jira_issue_key:
                jira_by_tc.setdefault(tc_id, set()).add(row.jira_issue_key)

    tc_summaries = []
    for tc in test_cases:
        latest_rr = latest_rr_by_tc.get(tc.id)
        stats = review_stats.get(latest_rr.id, {}) if latest_rr else {}
        pass_f   = stats.get("false_positive", 0)
        defect_f = stats.get("resolved", 0)
        open_f   = (latest_rr.errors or 0) + (latest_rr.warnings or 0) if latest_rr else 0
        total_obs = open_f + pass_f + defect_f
        jira_keys = sorted(jira_by_tc.get(tc.id, set()))
        tc_summaries.append({"tc": tc, "rr": latest_rr, "total_obs": total_obs,
                             "pass_f": pass_f, "defect_f": defect_f, "pending_f": open_f,
                             "jira_keys": jira_keys})

    # Aggregate dashboard metrics derived from tc_summaries
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)

    def _time_ago(dt):
        if not dt:
            return "Never"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = int((now_utc - dt).total_seconds())
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"

    for s in tc_summaries:
        s["time_ago"] = _time_ago(s["rr"].created_at if s["rr"] else None)

    total_open_defects = sum(s["defect_f"] for s in tc_summaries)
    total_pending      = sum(s["pending_f"] for s in tc_summaries)
    tests_passing   = sum(1 for s in tc_summaries if s["rr"] and s["rr"].status == "passed")
    tests_failing   = sum(1 for s in tc_summaries if s["rr"] and s["rr"].status == "failed")
    tests_in_review = sum(1 for s in tc_summaries if s["rr"] and s["rr"].status == "in_review")

    jira_config_active = bool(JiraConfig.query.filter_by(project_id=project_id, is_active=True).first())

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
        tc_summaries=tc_summaries,
        total_open_defects=total_open_defects,
        total_pending=total_pending,
        tests_passing=tests_passing,
        tests_failing=tests_failing,
        tests_in_review=tests_in_review,
        jira_config_active=jira_config_active,
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

    # Map form_id → list of test case names that depend on it (for delete UX)
    all_tcs = TestCase.query.filter_by(project_id=project_id).all()
    tc_by_form_id: dict = {}
    for tc in all_tcs:
        if tc.form_id:
            tc_by_form_id.setdefault(tc.form_id, []).append(tc.name)

    return render_template(
        "forms.html",
        page_title="FAST | Forms",
        project=project,
        forms=forms,
        tc_by_form_id=tc_by_form_id,
        max_allowed=MAX_FORMS_PER_PROJECT,
        user=session.get("user"),
        active="forms",
    )


@web_bp.route("/projects/<int:project_id>/forms/upload", methods=["POST"])
def upload_project_forms(project_id: int):
    gate = require_login() or require_role("admin")
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
    gate = require_login() or require_role("admin")
    if gate:
        return gate

    Project.query.get_or_404(project_id)
    form = Form.query.filter_by(id=form_id, project_id=project_id).first_or_404()

    blocking_tcs = TestCase.query.filter_by(form_id=form_id).all()
    force = request.form.get("force") == "1"

    if blocking_tcs and not force:
        names = ", ".join(f"'{tc.name}'" for tc in blocking_tcs[:3])
        extra = f" and {len(blocking_tcs) - 3} more" if len(blocking_tcs) > 3 else ""
        flash(
            f"Cannot delete — form is used by test case(s): {names}{extra}. "
            "Use Force Delete to remove the form along with those test cases.",
            "error",
        )
        return redirect(url_for("web.project_forms", project_id=project_id))

    if blocking_tcs and force:
        # Cascade-delete each blocking test case and its run data
        from app.models.finding_review import FindingReview as _FR
        from app.models.finding_comment import FindingComment as _FC
        for tc in blocking_tcs:
            rrs = RunResult.query.filter_by(test_case_id=tc.id).all()
            for rr in rrs:
                _FC.query.filter(
                    _FC.finding_review_id.in_(
                        db.session.query(_FR.id).filter_by(run_result_id=rr.id)
                    )
                ).delete(synchronize_session=False)
                _FR.query.filter_by(run_result_id=rr.id).delete(synchronize_session=False)
                db.session.delete(rr)
            db.session.delete(tc)
        flash(f"Form and {len(blocking_tcs)} test case(s) deleted.", "success")

    # Detach from any benchmark references
    for tc in TestCase.query.filter_by(benchmark_form_id=form_id).all():
        tc.benchmark_form_id = None

    store_dir = project_forms_dir(project_id)
    if form.stored_filename:
        file_path = os.path.join(store_dir, form.stored_filename)
        if os.path.exists(file_path):
            os.remove(file_path)

    db.session.delete(form)
    db.session.commit()
    if not blocking_tcs:
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
    gate = require_login() or require_role("admin")
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
    gate = require_login() or require_role("admin")
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


def _cleanup_stuck_runs(project_id: int) -> None:
    """Mark RunResults stuck in 'running' for >15 min as failed."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    stuck = RunResult.query.filter(
        RunResult.project_id == project_id,
        RunResult.status == "running",
        RunResult.created_at < cutoff,
    ).all()
    for rr in stuck:
        rr.status = "failed"
        if hasattr(rr, "error_message"):
            rr.error_message = "Run timed out — exceeded 15 minutes without completing."
    if stuck:
        db.session.commit()


@web_bp.route("/projects/<int:project_id>/execute/run", methods=["POST"])
def execute_run(project_id: int):
    gate = require_login() or require_role("reviewer")
    if gate:
        return gate

    Project.query.get_or_404(project_id)

    _cleanup_stuck_runs(project_id)

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

            rr.errors = int(out.get("errors") or 0) if hasattr(rr, "errors") else 0
            rr.warnings = int(out.get("warnings") or 0) if hasattr(rr, "warnings") else 0
            rr.passed = int(out.get("passed") or 0) if hasattr(rr, "passed") else 0

            result_obj = out.get("result_json") or {}
            if isinstance(result_obj, dict) and result_obj.get("error"):
                rr.status = "failed"
            elif (rr.errors or 0) > 0 or (rr.warnings or 0) > 0:
                # Findings exist — hold in human review until each is confirmed or dismissed.
                rr.status = "in_review"
                rr.passed = 0
            else:
                # No findings → automatically passed.
                rr.status = "passed"
                rr.passed = 1

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


@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/rerun_failures", methods=["POST"])
def rerun_failures(project_id: int, run_id: int):
    """Create a new run with only the failed/error test cases from a previous run."""
    gate = require_login() or require_role("reviewer")
    if gate:
        return gate

    run = Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()
    failed_rrs = RunResult.query.filter_by(run_id=run_id).filter(
        RunResult.status.in_(["failed", "error"])
    ).all()

    if not failed_rrs:
        flash("No failed test cases to rerun.", "info")
        return redirect(url_for("web.project_results", project_id=project_id, run_id=run_id))

    tc_ids = [rr.test_case_id for rr in failed_rrs if rr.test_case_id]
    test_cases = TestCase.query.filter(
        TestCase.project_id == project_id,
        TestCase.id.in_(tc_ids),
    ).all()

    if not test_cases:
        flash("Test cases for failed runs could not be found.", "error")
        return redirect(url_for("web.project_results", project_id=project_id, run_id=run_id))

    new_run = Run(
        project_id=project_id,
        triggered_by=f"{session.get('user')} (rerun of #{run_id})",
        status="running",
        total=len(test_cases),
    )
    db.session.add(new_run)
    db.session.commit()

    import threading
    from app.services.runner import run_testcase as _run_tc

    def _execute():
        import json as _json
        from app import create_app
        app = create_app()
        with app.app_context():
            for tc in test_cases:
                rr = RunResult(
                    run_id=new_run.id,
                    project_id=project_id,
                    test_case_id=tc.id,
                    form_id=tc.form_id,
                    mode=tc.mode,
                    status="running",
                )
                db.session.add(rr)
                db.session.commit()
                try:
                    out = _run_tc(project_id=project_id, tc=tc, run_id=new_run.id, rr_id=rr.id)
                    rr.result_json = _json.dumps(out.get("result_json") or {}, ensure_ascii=False)
                    rr.summary_text = out.get("summary_text") or ""
                    rr.errors = int(out.get("errors") or 0)
                    rr.warnings = int(out.get("warnings") or 0)
                    rr.passed = int(out.get("passed") or 0)
                    rr.status = "completed"
                except Exception as exc:
                    rr.status = "failed"
                    rr.error_message = str(exc)
                    rr.errors = 1
                db.session.add(rr)
                db.session.commit()
            new_run.status = "completed"
            db.session.commit()

    threading.Thread(target=_execute, daemon=True).start()

    flash(f"Re-running {len(test_cases)} failed test case(s) as Run #{new_run.id}.", "success")
    return redirect(url_for("web.project_results", project_id=project_id, run_id=new_run.id))


@web_bp.route("/projects/<int:project_id>/runs/<int:run_id>/delete", methods=["POST"])
def delete_run(project_id: int, run_id: int):
    gate = require_login() or require_role("admin")
    if gate:
        return gate

    run = Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()

    # Cascade: delete FindingComments → FindingReviews → RunResults → Run
    from app.models.finding_review import FindingReview as _FR
    from app.models.finding_comment import FindingComment as _FC
    rrs = RunResult.query.filter_by(run_id=run_id).all()
    for rr in rrs:
        _FC.query.filter(
            _FC.finding_review_id.in_(
                db.session.query(_FR.id).filter_by(run_result_id=rr.id)
            )
        ).delete(synchronize_session=False)
        _FR.query.filter_by(run_result_id=rr.id).delete(synchronize_session=False)
        db.session.delete(rr)
    db.session.delete(run)
    db.session.commit()

    flash(f"Run #{run_id} deleted.", "success")
    return redirect(url_for("web.project_results", project_id=project_id))


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
    test_cases = TestCase.query.filter_by(project_id=project_id).order_by(TestCase.id.asc()).all()

    # Build per-test-case trend datasets (last 30 runs each)
    tc_trend = {}
    recent_results = []
    for tc in test_cases:
        rrs = (
            RunResult.query
            .filter_by(project_id=project_id, test_case_id=tc.id)
            .order_by(RunResult.created_at.asc())
            .limit(30).all()
        )
        points = []
        for rr in rrs:
            obs = (rr.errors or 0) + (rr.warnings or 0)
            pts = {
                "rr_id": rr.id,
                "ts": rr.created_at.strftime("%Y-%m-%d %H:%M") if rr.created_at else "",
                "status": rr.status or "unknown",
                "obs": obs,
                "errors": rr.errors or 0,
                "warnings": rr.warnings or 0,
            }
            points.append(pts)
            recent_results.append({
                "rr_id": rr.id, "tc_name": tc.name, "tc_mode": tc.mode or "basic",
                "ts": rr.created_at, "status": rr.status or "unknown",
                "obs": obs, "errors": rr.errors or 0, "warnings": rr.warnings or 0,
                "project_id": project_id,
            })
        tc_trend[str(tc.id)] = {"name": tc.name, "mode": tc.mode or "basic", "data": points}

    recent_results.sort(key=lambda x: x["ts"] or __import__("datetime").datetime.min, reverse=True)

    return render_template(
        "trends.html",
        page_title=f"FAST | Trends — {project.name}",
        project=project,
        test_cases=test_cases,
        tc_trend_json=json.dumps(tc_trend),
        recent_results=recent_results[:60],
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

    # Prev / next result in the same run (ordered by id)
    run_results = RunResult.query.filter_by(run_id=rr.run_id).order_by(RunResult.id.asc()).all()
    run_result_ids = [r.id for r in run_results]
    current_pos = run_result_ids.index(result_id) if result_id in run_result_ids else -1
    prev_result_id = run_result_ids[current_pos - 1] if current_pos > 0 else None
    next_result_id = run_result_ids[current_pos + 1] if current_pos >= 0 and current_pos < len(run_result_ids) - 1 else None

    jira_config = JiraConfig.query.filter_by(project_id=project_id, is_active=True).first()

    # Build page→snapshot mapping from visual_validation entries so finding cards
    # can show a "View Diff" link when a visual diff image exists for that page.
    snapshot_by_page = {}
    for entry in result_obj.get("visual_validation") or []:
        snap = entry.get("snapshot_path")
        page = entry.get("page") or entry.get("actual_page_num")
        if snap and page is not None:
            snapshot_by_page[str(page)] = snap

    return render_template(
        "finding_reviews.html",
        page_title=f"FAST | Review Findings",
        project=project,
        rr=rr,
        tc=tc,
        findings=findings,
        reviewers=reviewers,
        compliance_standards=compliance_standards,
        jira_config=jira_config,
        snapshot_by_page=snapshot_by_page,
        prev_result_id=prev_result_id,
        next_result_id=next_result_id,
        current_pos=current_pos + 1,
        total_results=len(run_result_ids),
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


def _recompute_result_metrics(result_id: int) -> None:
    """Recompute errors/warnings/passed for a RunResult after a finding review action.

    Findings that have been resolved or marked false_positive are excluded from
    the error/warning count, updating the run dashboard immediately.
    """
    try:
        rr = RunResult.query.get(result_id)
        if not rr or not rr.result_json:
            return

        result_obj = json.loads(rr.result_json)

        # Same ordering used by result_reviews to build finding_index values.
        REVIEW_CATEGORIES = [
            "spelling_errors", "format_issues", "value_mismatches",
            "missing_content", "extra_content", "layout_anomalies",
            "compliance_issues", "visual_mismatches",
        ]
        ERROR_BUCKETS = {
            "spelling_errors", "format_issues", "value_mismatches",
            "missing_content", "compliance_issues", "visual_mismatches",
        }

        reviews = FindingReview.query.filter_by(run_result_id=result_id).all()
        dismissed = {r.finding_index for r in reviews if r.status in ("resolved", "false_positive")}

        global_idx = 0
        new_errors = 0
        new_warnings = 0
        for cat in REVIEW_CATEGORIES:
            for _item in result_obj.get(cat, []) or []:
                if global_idx not in dismissed:
                    if cat in ERROR_BUCKETS:
                        new_errors += 1
                    else:
                        new_warnings += 1
                global_idx += 1

        # Buckets not exposed in the review UI — always counted at face value.
        new_errors += len(result_obj.get("structural_changes", []) or [])
        new_warnings += len(result_obj.get("typography_issues", []) or [])
        new_warnings += len(result_obj.get("accessibility_issues", []) or [])

        rr.errors = new_errors
        rr.warnings = new_warnings

        # Determine the human-reviewed verdict.
        open_count = global_idx - len(dismissed)   # findings still awaiting review
        confirmed_defects = sum(
            1 for r in reviews if r.status == "resolved"
        )
        if open_count > 0:
            # Some findings not yet reviewed — keep in_review.
            rr.status = "in_review"
            rr.passed = 0
        elif confirmed_defects > 0:
            # Reviewer confirmed at least one real defect.
            rr.status = "failed"
            rr.passed = 0
        else:
            # All findings dismissed as false positives (or no findings).
            rr.status = "passed"
            rr.passed = 1

        # Propagate to the parent Run aggregate so dashboard KPIs update too.
        from app.models.run import Run
        run = Run.query.get(rr.run_id) if rr.run_id else None
        if run:
            all_rrs = RunResult.query.filter_by(run_id=rr.run_id).all()
            run.errors = sum(r.errors or 0 for r in all_rrs)
            run.warnings = sum(r.warnings or 0 for r in all_rrs)
            run.passed = sum(r.passed or 0 for r in all_rrs)

        db.session.commit()

        # Regenerate the full HTML report on final verdict so the chip, summary
        # and finding table all reflect the reviewed state correctly.
        if rr.status in ("passed", "failed"):
            try:
                from app.reporting.html_report import write_cli_style_report
                from app.models.test_case import TestCase as _TC
                from app.models.form import Form as _Form
                _tc = _TC.query.get(rr.test_case_id)
                _main_form = _Form.query.get(rr.form_id) if rr.form_id else None
                _bench_form = _Form.query.get(_tc.benchmark_form_id) if _tc and _tc.benchmark_form_id else None
                new_filename = write_cli_style_report(
                    project_id=rr.project_id,
                    run_id=rr.run_id,
                    rr_id=rr.id,
                    tc=_tc,
                    result_json=json.loads(rr.result_json) if rr.result_json else {},
                    llm_summary=rr.summary_text or "",
                    main_form=_main_form,
                    bench_form=_bench_form,
                )
                rr.report_html_path = new_filename
                db.session.commit()
            except Exception as _regen_err:
                logger.warning("Report regen failed: %s", _regen_err)
                # Fall back: patch verdict chip in existing report
                if rr.report_html_path:
                    try:
                        from app.reporting.html_report import _reports_dir
                        import re as _re
                        report_path = _reports_dir() / rr.report_html_path
                        if report_path.exists():
                            html = report_path.read_text(encoding="utf-8")
                            new_chip = "<span class='chip chip-ok'>PASS</span>" if rr.status == "passed" else "<span class='chip chip-bad'>FAIL</span>"
                            html = _re.sub(
                                r"<span class='chip chip-(?:ok|bad|warn)'>(?:PASS|FAIL|REVIEW|IN REVIEW)</span>",
                                new_chip, html, count=1,
                            )
                            report_path.write_text(html, encoding="utf-8")
                    except Exception as _chip_err:
                        logger.warning("Report chip fallback failed: %s", _chip_err)
    except Exception as _e:
        logger.warning("_recompute_result_metrics failed for result %d: %s", result_id, _e)


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/recompute", methods=["POST"])
def recompute_result_status(project_id: int, result_id: int):
    gate = require_login()
    if gate:
        return gate
    RunResult.query.get_or_404(result_id)
    _recompute_result_metrics(result_id)
    rr = RunResult.query.get(result_id)
    flash(f"Status updated to {rr.status}.", "success")
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

    # Recompute RunResult errors/warnings/passed after every review action
    # so the results dashboard reflects the dismissed findings immediately.
    _recompute_result_metrics(result_id)

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


@web_bp.route("/projects/<int:project_id>/results/<int:result_id>/reviews/log-jira", methods=["POST"])
def log_jira_defect(project_id: int, result_id: int):
    """One-click: create a Jira Cloud defect from a finding and save the issue key."""
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    jira_config = JiraConfig.query.filter_by(project_id=project_id, is_active=True).first()
    if not jira_config:
        flash("Jira integration is not configured for this project.", "error")
        return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id))

    finding_index = int(request.form.get("finding_index", 0))
    finding_category = request.form.get("finding_category", "")

    rr = RunResult.query.get_or_404(result_id)
    tc = TestCase.query.filter_by(id=rr.test_case_id, project_id=project_id).first()

    # Resolve the form filename for the defect description
    form_filename = "Unknown Form"
    if tc and tc.form_id:
        form_obj = Form.query.get(tc.form_id)
        if form_obj:
            form_filename = form_obj.original_filename or form_obj.name

    # Get the specific finding item from result_json
    item = {}
    if rr.result_json:
        try:
            result_obj = json.loads(rr.result_json)
            flat_index = 0
            categories = [
                "spelling_errors", "format_issues", "value_mismatches",
                "missing_content", "extra_content", "layout_anomalies",
                "compliance_issues", "visual_mismatches",
            ]
            for cat in categories:
                for entry in result_obj.get(cat, []):
                    if flat_index == finding_index:
                        item = entry
                        break
                    flat_index += 1
                if item:
                    break
        except Exception:
            pass

    # Create or get FindingReview record
    review = FindingReview.query.filter_by(
        run_result_id=result_id, finding_index=finding_index
    ).first()
    if review is None:
        review = FindingReview(
            run_result_id=result_id,
            project_id=project_id,
            finding_index=finding_index,
            finding_category=finding_category,
            finding_description=item.get("description", ""),
        )
        db.session.add(review)
        db.session.flush()

    # Idempotent: if already logged, just redirect
    if review.jira_issue_key:
        flash(f"Defect already logged: {review.jira_issue_key}", "info")
        return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id)
                        + f"#finding-{finding_index}")

    # Build Jira issue content
    page = item.get("page", "?")
    severity = item.get("severity", "unknown")
    category_display = finding_category.replace("_", " ").title()
    description_text = item.get("description", "No description available.")
    evidence_text = item.get("evidence", "")
    field_name = item.get("field_name", "")
    suggestion_text = item.get("suggestion", "")

    summary = f"[FAST] {category_display} \u2013 {form_filename} (Page {page})"

    # Project context
    project_obj = Project.query.get(project_id)
    proj_name = project_obj.name if project_obj else f"Project #{project_id}"
    proj_env = (project_obj.environment or "").strip() if project_obj else ""
    proj_account = (project_obj.account or "").strip() if project_obj else ""

    # Benchmark form (benchmark mode only)
    benchmark_filename = None
    if tc and tc.benchmark_form_id:
        bench_form = Form.query.get(tc.benchmark_form_id)
        if bench_form:
            benchmark_filename = bench_form.original_filename or bench_form.name

    # Run date in ET
    run_date_str = ""
    if rr.created_at:
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone as _tz
            dt = rr.created_at.replace(tzinfo=_tz.utc)
            run_date_str = dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d at %H:%M ET")
        except Exception:
            run_date_str = rr.created_at.strftime("%Y-%m-%d at %H:%M UTC")

    tc_name = tc.name if tc else f"Test Case #{rr.test_case_id}"

    description_lines = [
        "\u2501" * 38,
        "FAST AI Forms Testing \u2014 Defect Report",
        "\u2501" * 38,
        "",
        "PROJECT CONTEXT",
        f"Project:     {proj_name}",
    ]
    if proj_env:
        description_lines.append(f"Environment: {proj_env}")
    if proj_account:
        description_lines.append(f"Account:     {proj_account}")

    description_lines += [
        "",
        "FORMS TESTED",
        f"Current Form:  {form_filename}",
    ]
    if benchmark_filename:
        description_lines.append(f"Benchmark Form: {benchmark_filename}")
    description_lines += [
        f"Test Mode:     {rr.mode}",
        f"Test Case:     {tc_name}",
        "",
        "ANOMALY DETAILS",
        f"Severity:  {severity.upper()}",
        f"Category:  {category_display}",
        f"Page:      {page}",
    ]
    if field_name:
        description_lines.append(f"Field:     {field_name}")

    description_lines += [
        "",
        "DESCRIPTION",
        description_text,
    ]
    if evidence_text:
        description_lines += ["", "EVIDENCE", evidence_text]
    if suggestion_text:
        description_lines += ["", "SUGGESTION", suggestion_text]

    description_lines += [
        "",
        "RUN CONTEXT",
        f"Run:      #{rr.run_id}  (Result #{rr.id})",
    ]
    if run_date_str:
        description_lines.append(f"Run Date: {run_date_str}")
    description_lines += [
        f"Health:   {rr.errors or 0} errors | {rr.warnings or 0} warnings | {rr.passed or 0} passed",
        "",
        "\u2501" * 38,
        "Logged by FAST \u2013 AI Assisted Forms Testing",
    ]

    description_body = "\n".join(description_lines)

    try:
        issue_key = jira_client.create_issue(jira_config, summary, description_body)
    except Exception as exc:
        flash(f"Failed to create Jira issue: {exc}", "error")
        return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id)
                        + f"#finding-{finding_index}")

    review.jira_issue_key = issue_key
    db.session.commit()

    log_action(
        action="jira.defect_created",
        resource_type="finding_review",
        resource_id=review.id,
        detail={"issue_key": issue_key, "finding_index": finding_index, "result_id": result_id},
        project_id=project_id,
        username=session.get("user"),
    )

    flash(f"Jira defect created: {issue_key}", "success")
    return redirect(url_for("web.result_reviews", project_id=project_id, result_id=result_id)
                    + f"#finding-{finding_index}")


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
               project_id=project_id, detail={"run_id": run_id, "decision": decision},
               username=session.get("user"))
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
# Jira Integration Settings
# -----------------------

@web_bp.route("/projects/<int:project_id>/jira", methods=["GET", "POST"])
def project_jira_settings(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    project = Project.query.get_or_404(project_id)
    config = JiraConfig.query.filter_by(project_id=project_id).first()

    if request.method == "POST":
        jira_url = request.form.get("jira_url", "").strip().rstrip("/")
        email = request.form.get("email", "").strip()
        api_token = request.form.get("api_token", "").strip()
        jira_project_key = request.form.get("jira_project_key", "").strip().upper()
        issue_type = request.form.get("issue_type", "Bug").strip() or "Bug"
        is_active = request.form.get("is_active") == "1"

        if not jira_url or not email or not jira_project_key:
            flash("Jira URL, email, and project key are required.", "error")
        else:
            if config is None:
                config = JiraConfig(
                    project_id=project_id,
                    created_by=session.get("user"),
                )
                db.session.add(config)

            config.jira_url = jira_url
            config.email = email
            if api_token:  # Only update token if a new one was entered
                config.api_token = api_token
            config.jira_project_key = jira_project_key
            config.issue_type = issue_type
            config.is_active = is_active
            db.session.commit()

            log_action(
                action="jira.config_saved",
                resource_type="jira_config",
                resource_id=config.id,
                detail={"jira_url": jira_url, "jira_project_key": jira_project_key},
                project_id=project_id,
                username=session.get("user"),
            )
            flash("Jira settings saved.", "success")

        return redirect(url_for("web.project_jira_settings", project_id=project_id))

    # Fetch available issue types from Jira so the template can show a dropdown
    issue_types = []
    if config and config.is_active:
        try:
            issue_types = jira_client.fetch_issue_types(config)
        except Exception:
            pass

    return render_template(
        "jira_settings.html",
        page_title="FAST | Jira Integration",
        project=project,
        config=config,
        issue_types=issue_types,
        active="jira",
        user=session.get("user"),
        user_role=session.get("role", "viewer"),
    )


@web_bp.route("/projects/<int:project_id>/jira/status", methods=["GET"])
def jira_issue_status(project_id: int):
    """Return live Jira status for a comma-separated list of issue keys.

    Query param: ?keys=KAN-1,KAN-2
    Returns JSON: {"KAN-1": {"name": "Done", "category": "done"}, ...}
    """
    gate = require_login()
    if gate:
        return gate

    config = JiraConfig.query.filter_by(project_id=project_id, is_active=True).first()
    if not config:
        return jsonify({}), 200

    raw_keys = request.args.get("keys", "")
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not keys:
        return jsonify({}), 200

    statuses = jira_client.fetch_issue_statuses(config, keys)
    return jsonify(statuses)


@web_bp.route("/projects/<int:project_id>/jira/test", methods=["POST"])
def test_jira_connection(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("reviewer")
    if role_check:
        return role_check

    config = JiraConfig.query.filter_by(project_id=project_id).first()
    if not config:
        flash("No Jira configuration saved yet.", "error")
        return redirect(url_for("web.project_jira_settings", project_id=project_id))

    success, message = jira_client.test_connection(config)
    flash(message, "success" if success else "error")
    return redirect(url_for("web.project_jira_settings", project_id=project_id))


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
    all_projects = Project.query.order_by(Project.name).all()
    all_members = ProjectMember.query.all()
    # {project_id: [User, ...]}
    members_by_project = {}
    member_user_ids = {m.user_id for m in all_members}
    member_users = {u.id: u for u in User.query.filter(User.id.in_(member_user_ids)).all()} if member_user_ids else {}
    for m in all_members:
        members_by_project.setdefault(m.project_id, []).append(member_users.get(m.user_id))

    return render_template(
        "admin_panel.html",
        page_title="FAST | Admin",
        users=users,
        audit_logs=audit_logs,
        api_keys=api_keys,
        total_projects=total_projects,
        total_runs=total_runs,
        total_forms=total_forms,
        all_projects=all_projects,
        members_by_project=members_by_project,
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


@web_bp.route("/admin/projects/<int:project_id>/members/add", methods=["POST"])
def admin_add_project_member(project_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    user_id = request.form.get("user_id", type=int)
    if not user_id:
        flash("No user selected.", "error")
        return redirect(url_for("web.admin_panel"))
    project = Project.query.get_or_404(project_id)
    user = User.query.get_or_404(user_id)
    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
    if not existing:
        db.session.add(ProjectMember(project_id=project_id, user_id=user_id))
        db.session.commit()
        flash(f"Added {user.username} to {project.name}.", "success")
    else:
        flash(f"{user.username} is already a member of {project.name}.", "info")
    return redirect(url_for("web.admin_panel"))


@web_bp.route("/admin/projects/<int:project_id>/members/<int:user_id>/remove", methods=["POST"])
def admin_remove_project_member(project_id: int, user_id: int):
    gate = require_login()
    if gate:
        return gate
    role_check = require_role("admin")
    if role_check:
        return role_check

    pm = ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
    if pm:
        db.session.delete(pm)
        db.session.commit()
        flash("Member removed.", "success")
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
