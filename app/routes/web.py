# app/routes/web.py
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
from app.services.runner import run_testcase


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

        session["user"] = username
        return redirect(url_for("web.home"))

    if is_logged_in():
        return redirect(url_for("web.home"))

    return render_template("landing.html", page_title="FAST | Login")


@web_bp.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("web.landing"))


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

    projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template(
        "home.html",
        page_title="FAST | Project Dashboard",
        projects=projects,
        user=session.get("user"),
        active="home",
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

        p = Project(name=name, description=description)
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

    return render_template(
        "project_overview.html",
        page_title=f"FAST | {project.name}",
        project=project,
        last_run=last_run,
        last_summary=last_summary,
        recent_runs=recent_runs,
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

    selected_run = None
    results = []
    if run_id:
        selected_run = Run.query.filter_by(id=run_id, project_id=project_id).first()
        if selected_run:
            results = RunResult.query.filter_by(run_id=run_id).order_by(RunResult.id.asc()).all()

    return render_template(
        "results.html",
        page_title="FAST | Results",
        project=project,
        runs=runs,
        selected_run=selected_run,
        results=results,
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
