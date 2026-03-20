"""
Webhook delivery service.
Fires HTTP POST to registered endpoints on events like run.completed.
Supports HMAC-SHA256 signing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def fire_event(event_name: str, payload: Dict[str, Any], project_id: int) -> None:
    """
    Deliver a webhook event to all active, matching endpoints for a project.
    Called synchronously — consider running in a thread for production.
    """
    try:
        import requests
        from flask import current_app
        from app.models.webhook_config import WebhookConfig
        from app.extensions import db

        webhooks = WebhookConfig.query.filter_by(project_id=project_id, is_active=True).all()
        for wh in webhooks:
            if event_name not in wh.event_list():
                continue

            body = json.dumps({
                "event": event_name,
                "project_id": project_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "payload": payload,
            }).encode("utf-8")

            headers = {
                "Content-Type": "application/json",
                "X-FAST-Event": event_name,
                "X-FAST-Project": str(project_id),
            }
            if wh.secret:
                sig = _sign_payload(wh.secret, body)
                headers["X-FAST-Signature"] = f"sha256={sig}"

            try:
                resp = requests.post(wh.url, data=body, headers=headers, timeout=10)
                wh.last_triggered_at = datetime.utcnow()
                wh.last_status_code = resp.status_code
                db.session.commit()
                logger.info("Webhook %s → %s status=%s", event_name, wh.url[:60], resp.status_code)
            except Exception as exc:
                logger.warning("Webhook delivery failed to %s: %s", wh.url[:60], exc)
                wh.last_triggered_at = datetime.utcnow()
                wh.last_status_code = 0
                db.session.commit()
    except Exception as exc:
        logger.error("fire_event error: %s", exc)
