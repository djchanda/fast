"""
Multi-model LLM client supporting Gemini, OpenAI GPT-4, and Anthropic Claude.
Model is selected via the LLM_PROVIDER env var (default: gemini).
"""
import os
import json
import re
import time
import logging
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON helpers (shared)
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    text = text.strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    if not text:
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


def _safe_json_loads(raw: str) -> Dict[str, Any]:
    raw = _strip_code_fences(raw)
    try:
        return json.loads(raw)
    except Exception:
        candidate = _extract_json_object(raw)
        try:
            return json.loads(candidate)
        except Exception:
            # LLM returned non-JSON — wrap as error so callers can handle gracefully
            return {"error": "LLM returned non-JSON response", "raw_response": raw[:500]}


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors worth retrying."""
    msg = str(exc).lower()
    retryable_signals = ("timeout", "timed out", "rate limit", "429", "503",
                         "connection", "network", "overloaded", "unavailable")
    return any(s in msg for s in retryable_signals)


def _call_with_retry(fn, messages: List[Dict], max_retries: int = 2) -> Dict[str, Any]:
    """Call fn(messages) with exponential-backoff retry for transient failures."""
    delay = 4.0
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(messages)
        except ValueError:
            raise  # config errors (missing API key) — don't retry
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries and _is_retryable(exc):
                logger.warning("LLM transient error (attempt %d/%d): %s — retrying in %.0fs",
                               attempt + 1, max_retries + 1, exc, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise
    raise last_exc  # unreachable but satisfies type checkers


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _run_gemini(messages: List[Dict]) -> Dict[str, Any]:
    import google.generativeai as genai

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY in environment")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    prompt_text = ""
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        prompt_text += f"{role}:\n{content}\n\n"

    response = model.generate_content(
        prompt_text,
        generation_config={
            "temperature": 0.0,
            "response_mime_type": "application/json",
        },
        request_options={"timeout": 180},
    )
    raw = getattr(response, "text", "") or ""
    return _safe_json_loads(raw)


def _run_openai(messages: List[Dict]) -> Dict[str, Any]:
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o")
    if not api_key:
        raise ValueError("Missing OPENAI_API_KEY in environment")

    client = OpenAI(api_key=api_key, timeout=180.0)
    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or ""
    return _safe_json_loads(raw)


def _run_claude(messages: List[Dict]) -> Dict[str, Any]:
    import anthropic
    import httpx

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model_name = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    if not api_key:
        raise ValueError("Missing ANTHROPIC_API_KEY in environment")

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(180.0, connect=15.0),
    )

    # Split system message from user messages
    system_msg = ""
    user_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msg += msg.get("content", "") + "\n"
        else:
            user_messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    if not user_messages:
        user_messages = [{"role": "user", "content": system_msg}]
        system_msg = ""

    kwargs = dict(
        model=model_name,
        max_tokens=8096,
        temperature=0.0,
        messages=user_messages,
    )
    if system_msg.strip():
        kwargs["system"] = system_msg.strip()

    response = client.messages.create(**kwargs)
    raw = response.content[0].text if response.content else ""
    return _safe_json_loads(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PROVIDERS = {
    "gemini": _run_gemini,
    "openai": _run_openai,
    "claude": _run_claude,
    "anthropic": _run_claude,
}


def run_validation(messages: List[Dict], provider: str = None) -> Dict[str, Any]:
    """
    Run form validation via the configured LLM provider.

    Args:
        messages: OpenAI-style chat messages list.
        provider: Override the LLM_PROVIDER env var. One of: gemini, openai, claude.

    Returns:
        dict with validation findings.
    """
    if not provider:
        provider = os.getenv("LLM_PROVIDER", "gemini").lower()

    fn = PROVIDERS.get(provider)
    if not fn:
        return {
            "error": f"Unknown LLM provider: {provider}",
            "details": f"Supported providers: {', '.join(PROVIDERS.keys())}",
        }

    prompt_text = " ".join(m.get("content", "") for m in messages)
    if not prompt_text.strip():
        return {"error": "Validation aborted", "details": "Prompt was empty."}

    try:
        return _call_with_retry(fn, messages)
    except ValueError as e:
        # Config error (missing API key) — surface directly
        return {"error": f"LLM configuration error ({provider})", "details": str(e)}
    except Exception as e:
        logger.error("LLM request failed (%s) after retries: %s", provider, e, exc_info=True)
        return {"error": f"LLM request failed ({provider})", "details": str(e)}


def get_available_providers() -> List[str]:
    """Return list of providers that have API keys configured."""
    available = []
    if os.getenv("GEMINI_API_KEY"):
        available.append("gemini")
    if os.getenv("OPENAI_API_KEY"):
        available.append("openai")
    if os.getenv("ANTHROPIC_API_KEY"):
        available.append("claude")
    return available or ["gemini"]
