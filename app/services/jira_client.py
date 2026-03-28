"""Jira Cloud REST API v3 client for FAST defect logging."""

import base64
import logging

import requests

logger = logging.getLogger(__name__)


def _auth_header(config) -> str:
    """Return a Basic auth header value for the given JiraConfig."""
    token = base64.b64encode(f"{config.email}:{config.api_token}".encode()).decode()
    return f"Basic {token}"


def _headers(config) -> dict:
    return {
        "Authorization": _auth_header(config),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_issue(config, summary: str, description_text: str) -> str:
    """Create a Jira Cloud issue and return its key (e.g. 'PROJ-42').

    Uses Atlassian Document Format (ADF) for the description body, which is
    required by the Jira Cloud REST API v3.

    Raises requests.HTTPError on non-2xx responses.
    """
    # Build ADF description — each line as a separate paragraph for readability
    paragraphs = []
    for line in description_text.split("\n"):
        paragraphs.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": line if line else " "}],
        })

    payload = {
        "fields": {
            "project": {"key": config.jira_project_key},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": paragraphs,
            },
            "issuetype": {"name": config.issue_type or "Bug"},
        }
    }

    resp = requests.post(
        f"{config.jira_url.rstrip('/')}/rest/api/3/issue",
        json=payload,
        headers=_headers(config),
        timeout=15,
    )
    resp.raise_for_status()
    issue_key = resp.json()["key"]
    logger.info("Jira issue created: %s", issue_key)
    return issue_key


def test_connection(config) -> tuple[bool, str]:
    """Verify Jira credentials by calling /rest/api/3/myself.

    Returns (success: bool, message: str).
    """
    try:
        resp = requests.get(
            f"{config.jira_url.rstrip('/')}/rest/api/3/myself",
            headers=_headers(config),
            timeout=10,
        )
        if resp.status_code == 200:
            display = resp.json().get("displayName", "")
            return True, f"Connected as {display}"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as exc:
        return False, str(exc)
