"""
REST API — API-first access to FAST validation.

Authentication: Bearer token (API key) in Authorization header.
All endpoints return JSON.

Base path: /api/v1/
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime
from functools import wraps
from typing import Optional, Tuple

from flask import Blueprint, jsonify, request, current_app

from app.extensions import db

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _authenticate() -> Tuple[Optional[object], Optional[object]]:
    """Return (api_key_record, error_response)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, (jsonify({"error": "Missing or invalid Authorization header"}), 401)

    raw_key = auth[len("Bearer "):]
    from app.models.api_key import ApiKey

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = ApiKey.query.filter_by(key_hash=key_hash, is_active=True).first()

    if not api_key:
        return None, (jsonify({"error": "Invalid API key"}), 401)

    if api_key.expires_at and api_key.expires_at < datetime.utcnow():
        return None, (jsonify({"error": "API key expired"}), 401)

    api_key.last_used_at = datetime.utcnow()
    db.session.commit()
    return api_key, None


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key, err = _authenticate()
        if err:
            return err
        return f(*args, api_key=api_key, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@api_bp.route("/projects", methods=["GET"])
@require_api_key
def list_projects(api_key):
    from app.models.project import Project
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([{
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "created_at": p.created_at.isoformat(),
    } for p in projects])


@api_bp.route("/projects/<int:project_id>", methods=["GET"])
@require_api_key
def get_project(project_id, api_key):
    from app.models.project import Project
    from app.models.run import Run
    p = Project.query.get_or_404(project_id)
    last_run = Run.query.filter_by(project_id=project_id).order_by(Run.created_at.desc()).first()
    return jsonify({
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "created_at": p.created_at.isoformat(),
        "last_run": {
            "id": last_run.id,
            "status": last_run.status,
            "errors": last_run.errors,
            "warnings": last_run.warnings,
            "passed": last_run.passed,
            "created_at": last_run.created_at.isoformat(),
        } if last_run else None,
    })


# ---------------------------------------------------------------------------
# Validate (submit PDF for one-shot validation)
# ---------------------------------------------------------------------------

@api_bp.route("/validate", methods=["POST"])
@require_api_key
def validate_pdf(api_key):
    """
    One-shot PDF validation endpoint.

    Multipart form fields:
      - file: PDF file to validate (required)
      - mode: basic | specific | benchmark (default: basic)
      - prompt: validation rules for specific mode
      - provider: gemini | openai | claude (optional, uses LLM_PROVIDER env var)
      - benchmark_file: golden copy PDF for benchmark mode

    Returns: JSON validation findings
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided. Send 'file' as multipart form field."}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF."}), 400

    mode = (request.form.get("mode") or "basic").lower()
    if mode not in ("basic", "specific", "benchmark"):
        return jsonify({"error": "mode must be basic, specific, or benchmark"}), 400

    prompt = request.form.get("prompt") or ""
    provider = request.form.get("provider") or None

    pdf_bytes = f.read()

    benchmark_bytes = None
    if mode == "benchmark":
        if "benchmark_file" not in request.files:
            return jsonify({"error": "benchmark_file is required for benchmark mode"}), 400
        bf = request.files["benchmark_file"]
        benchmark_bytes = bf.read()

    try:
        from engine.extractor import extract_all
        from engine.prompt_builder import build_prompt
        from engine.llm_client import run_validation

        current_doc = extract_all(pdf_bytes)
        benchmark_doc = extract_all(benchmark_bytes) if benchmark_bytes else None

        messages = build_prompt(
            mode=mode,
            current_doc=current_doc,
            benchmark_doc=benchmark_doc,
            base_prompt=prompt,
        )

        result = run_validation(messages, provider=provider)
        return jsonify({
            "mode": mode,
            "provider": provider or os.getenv("LLM_PROVIDER", "gemini"),
            "findings": result,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@api_bp.route("/projects/<int:project_id>/runs", methods=["GET"])
@require_api_key
def list_runs(project_id, api_key):
    from app.models.run import Run
    runs = Run.query.filter_by(project_id=project_id).order_by(Run.created_at.desc()).limit(50).all()
    return jsonify([{
        "id": r.id,
        "status": r.status,
        "total": r.total,
        "errors": r.errors,
        "warnings": r.warnings,
        "passed": r.passed,
        "triggered_by": r.triggered_by,
        "created_at": r.created_at.isoformat(),
    } for r in runs])


@api_bp.route("/projects/<int:project_id>/runs/<int:run_id>", methods=["GET"])
@require_api_key
def get_run(project_id, run_id, api_key):
    from app.models.run import Run
    from app.models.run_result import RunResult
    run = Run.query.filter_by(id=run_id, project_id=project_id).first_or_404()
    results = RunResult.query.filter_by(run_id=run_id).all()
    return jsonify({
        "id": run.id,
        "status": run.status,
        "total": run.total,
        "errors": run.errors,
        "warnings": run.warnings,
        "passed": run.passed,
        "triggered_by": run.triggered_by,
        "created_at": run.created_at.isoformat(),
        "results": [{
            "id": rr.id,
            "test_case_id": rr.test_case_id,
            "mode": rr.mode,
            "status": rr.status,
            "errors": rr.errors,
            "warnings": rr.warnings,
            "passed": rr.passed,
            "findings": json.loads(rr.result_json) if rr.result_json else {},
        } for rr in results],
    })


# ---------------------------------------------------------------------------
# Evidence Bundle
# ---------------------------------------------------------------------------

@api_bp.route("/projects/<int:project_id>/runs/<int:run_id>/bundle", methods=["GET"])
@require_api_key
def download_bundle(project_id, run_id, api_key):
    """Download a ZIP evidence bundle for a run."""
    from app.services.evidence_bundle import build_evidence_bundle
    from flask import send_file
    import io

    try:
        bundle_bytes = build_evidence_bundle(run_id=run_id, project_id=project_id)
        return send_file(
            io.BytesIO(bundle_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"evidence_bundle_run_{run_id}.zip",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API Key Management
# ---------------------------------------------------------------------------

@api_bp.route("/keys", methods=["POST"])
def create_api_key():
    """Create a new API key (requires session login, not API key auth)."""
    from flask import session
    from app.models.api_key import ApiKey

    if not session.get("user"):
        return jsonify({"error": "Login required"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    scopes = data.get("scopes", "validate:read,projects:read")

    raw_key, prefix, key_hash = ApiKey.generate()
    api_key = ApiKey(
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
        scopes=scopes,
        created_by=session.get("user"),
        is_active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({
        "id": api_key.id,
        "name": api_key.name,
        "key": raw_key,   # Only shown once
        "prefix": prefix,
        "scopes": scopes,
        "created_at": api_key.created_at.isoformat(),
        "warning": "Save this key — it will not be shown again.",
    }), 201


@api_bp.route("/keys", methods=["GET"])
def list_api_keys():
    from flask import session
    from app.models.api_key import ApiKey

    if not session.get("user"):
        return jsonify({"error": "Login required"}), 401

    keys = ApiKey.query.filter_by(is_active=True).order_by(ApiKey.created_at.desc()).all()
    return jsonify([{
        "id": k.id,
        "name": k.name,
        "prefix": k.key_prefix,
        "scopes": k.scopes,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat(),
    } for k in keys])


@api_bp.route("/keys/<int:key_id>", methods=["DELETE"])
def revoke_api_key(key_id):
    from flask import session
    from app.models.api_key import ApiKey

    if not session.get("user"):
        return jsonify({"error": "Login required"}), 401

    key = ApiKey.query.get_or_404(key_id)
    key.is_active = False
    db.session.commit()
    return jsonify({"message": f"API key {key_id} revoked."})


# ---------------------------------------------------------------------------
# Trend data
# ---------------------------------------------------------------------------

@api_bp.route("/projects/<int:project_id>/trends", methods=["GET"])
@require_api_key
def get_trends(project_id, api_key):
    """Return error/warning/passed trends across last N runs."""
    from app.models.run import Run
    limit = request.args.get("limit", 20, type=int)
    runs = Run.query.filter_by(project_id=project_id).order_by(Run.created_at.asc()).limit(limit).all()
    return jsonify({
        "project_id": project_id,
        "runs": [{
            "run_id": r.id,
            "created_at": r.created_at.isoformat(),
            "errors": r.errors,
            "warnings": r.warnings,
            "passed": r.passed,
            "total": r.total,
            "status": r.status,
        } for r in runs]
    })


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@api_bp.route("/projects/<int:project_id>/webhooks", methods=["GET"])
@require_api_key
def list_webhooks(project_id, api_key):
    from app.models.webhook_config import WebhookConfig
    whs = WebhookConfig.query.filter_by(project_id=project_id).all()
    return jsonify([{
        "id": w.id,
        "name": w.name,
        "url": w.url,
        "events": w.events,
        "is_active": w.is_active,
        "last_triggered_at": w.last_triggered_at.isoformat() if w.last_triggered_at else None,
        "last_status_code": w.last_status_code,
    } for w in whs])


@api_bp.route("/projects/<int:project_id>/webhooks", methods=["POST"])
@require_api_key
def create_webhook(project_id, api_key):
    from app.models.webhook_config import WebhookConfig
    from flask import session
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    wh = WebhookConfig(
        project_id=project_id,
        name=data.get("name", "Webhook"),
        url=url,
        secret=data.get("secret"),
        events=data.get("events", "run.completed"),
        created_by=session.get("user"),
    )
    db.session.add(wh)
    db.session.commit()
    return jsonify({"id": wh.id, "url": wh.url, "events": wh.events}), 201
