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

    Raises RuntimeError with a human-readable message on failure.
    """
    # Build ADF description — skip empty lines (whitespace-only text nodes are rejected by Jira)
    paragraphs = []
    for line in description_text.split("\n"):
        if line.strip():
            paragraphs.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            })
        else:
            # empty paragraph for blank lines — valid ADF, no text node
            paragraphs.append({"type": "paragraph", "content": []})

    payload = {
        "fields": {
            "project": {"key": config.jira_project_key},
            "summary": summary[:255],  # Jira enforces 255-char limit
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

    if resp.status_code == 201:
        issue_key = resp.json()["key"]
        logger.info("Jira issue created: %s", issue_key)
        return issue_key

    # Extract Jira's detailed error message from the response body
    try:
        err = resp.json()
        msgs = err.get("errorMessages", [])
        field_errs = list(err.get("errors", {}).values())
        detail = "; ".join(msgs + field_errs) or f"HTTP {resp.status_code}"
    except Exception:
        detail = resp.text[:300] or f"HTTP {resp.status_code}"

    raise RuntimeError(f"Jira API error ({resp.status_code}): {detail}")


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


def fetch_issue_types(config) -> list:
    """Return available issue type names for the configured Jira project.

    Calls GET /rest/api/3/project/{key} and extracts issueTypes[].name,
    excluding subtask types. Returns empty list on any failure.
    """
    try:
        resp = requests.get(
            f"{config.jira_url.rstrip('/')}/rest/api/3/project/{config.jira_project_key}",
            headers=_headers(config),
            timeout=10,
        )
        if resp.status_code == 200:
            types = resp.json().get("issueTypes", [])
            return [t["name"] for t in types if not t.get("subtask", False)]
    except Exception:
        pass
    return []


def fetch_issue_statuses(config, keys: list[str]) -> dict:
    """Return status info for a list of Jira issue keys.

    Returns: {key: {"name": str, "category": str}} where category is one of
    "done", "inprogress", "new" (maps to Jira's statusCategory keys).
    Keys that fail or don't exist are omitted from the result.
    """
    result = {}
    for key in keys:
        try:
            resp = requests.get(
                f"{config.jira_url.rstrip('/')}/rest/api/3/issue/{key}?fields=status",
                headers=_headers(config),
                timeout=8,
            )
            if resp.status_code == 200:
                status = resp.json().get("fields", {}).get("status", {})
                result[key] = {
                    "name": status.get("name", ""),
                    "category": (status.get("statusCategory") or {}).get("key", "new"),
                }
        except Exception:
            pass
    return result
