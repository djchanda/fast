"""Audit log helper — wraps writing to AuditLog cleanly."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from flask import request


def log_action(
    action: str,
    resource_type: str = None,
    resource_id: int = None,
    project_id: int = None,
    detail: Dict[str, Any] = None,
    username: str = None,
) -> None:
    """Write an audit log entry. Silently swallows errors to avoid breaking flows."""
    try:
        from app.models.audit_log import AuditLog
        from app.extensions import db
        from flask import session

        user = username or session.get("user") or "system"
        ip = None
        try:
            ip = request.remote_addr
        except Exception:
            pass

        entry = AuditLog(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            username=user,
            user_ip=ip,
            detail=json.dumps(detail) if detail else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        pass
