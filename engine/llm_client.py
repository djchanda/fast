"""
LLM Client — multi-provider with Bedrock as the AWS-native option.

Providers:
  bedrock  — AWS Bedrock (Claude via IAM, no API key, VPC endpoint safe)
  claude   — Anthropic direct API
  gemini   — Google Gemini
  openai   — OpenAI GPT-4o

The caller uses run_llm(messages) and never touches provider internals.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json(text: str) -> str:
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def _parse_json(raw: str) -> dict[str, Any]:
    raw = _strip_fences(raw)
    try:
        return json.loads(raw)
    except Exception:
        try:
            return json.loads(_extract_json(raw))
        except Exception:
            return {"raw_response": raw[:500], "error": "non-JSON response from LLM"}


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    signals = ("timeout", "rate limit", "429", "503", "504", "overloaded",
                "connection", "network", "unavailable", "stream cancelled")
    return any(s in msg for s in signals)


def _retry(fn, messages, max_retries: int = 3) -> dict[str, Any]:
    delay = 5.0
    for attempt in range(max_retries + 1):
        try:
            return fn(messages)
        except ValueError:
            raise
        except Exception as exc:
            if attempt < max_retries and _is_retryable(exc):
                logger.warning("LLM transient error attempt %d/%d: %s — retry in %.0fs",
                               attempt + 1, max_retries + 1, exc, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------

def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


# ---------------------------------------------------------------------------
# AWS Bedrock (preferred for BMO / AWS deployment)
# ---------------------------------------------------------------------------

def _run_bedrock(messages: list[dict]) -> dict[str, Any]:
    """
    Call Claude on AWS Bedrock using IAM role auth (no API key required).
    Traffic routed via VPC endpoint — never touches public internet.
    """
    try:
        import boto3
    except ImportError as e:
        raise ImportError("boto3 not installed. Run: pip install boto3") from e

    from config import llm_config
    cfg = llm_config()

    client = boto3.client("bedrock-runtime", region_name=cfg.bedrock_region)

    system_parts = []
    user_messages = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            text = _content_to_text(content) if isinstance(content, list) else content
            system_parts.append({"text": text})
            continue

        # Translate content blocks to Bedrock format
        if isinstance(content, list):
            bedrock_content = []
            for block in content:
                if block.get("type") == "text":
                    bedrock_content.append({"text": block["text"]})
                elif block.get("type") == "image":
                    label = block.get("label", "")
                    if label:
                        bedrock_content.append({"text": f"[{label}]"})
                    import base64
                    bedrock_content.append({
                        "image": {
                            "format": block.get("mime", "image/jpeg").split("/")[-1],
                            "source": {
                                "bytes": base64.standard_b64decode(block["b64"])
                            },
                        }
                    })
            user_messages.append({"role": role, "content": bedrock_content})
        else:
            user_messages.append({"role": role, "content": [{"text": content}]})

    if not user_messages:
        user_messages = [{"role": "user", "content": [{"text": "\n".join(p["text"] for p in system_parts)}]}]
        system_parts = []

    body: dict[str, Any] = {
        "anthropicVersion": "bedrock-2023-05-31",
        "max_tokens": 8096,
        "temperature": 0.0,
        "messages": user_messages,
    }
    if system_parts:
        body["system"] = system_parts

    response = client.invoke_model(
        modelId=cfg.bedrock_model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    raw = result.get("content", [{}])[0].get("text", "")
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Anthropic direct
# ---------------------------------------------------------------------------

def _run_claude(messages: list[dict]) -> dict[str, Any]:
    try:
        import anthropic, httpx
    except ImportError as e:
        raise ImportError("anthropic SDK not installed. Run: pip install anthropic") from e

    from config import llm_config
    cfg = llm_config()
    if not cfg.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(
        api_key=cfg.anthropic_api_key,
        timeout=httpx.Timeout(cfg.timeout_secs, connect=15.0),
    )

    system_msg = ""
    user_messages = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            system_msg += (_content_to_text(content) if isinstance(content, list) else content) + "\n"
            continue

        if isinstance(content, list):
            ant_content = []
            for block in content:
                if block.get("type") == "text":
                    ant_content.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    if block.get("label"):
                        ant_content.append({"type": "text", "text": f"[{block['label']}]"})
                    ant_content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": block.get("mime", "image/jpeg"), "data": block["b64"]},
                    })
            user_messages.append({"role": role, "content": ant_content})
        else:
            user_messages.append({"role": role, "content": content})

    if not user_messages:
        user_messages = [{"role": "user", "content": system_msg}]
        system_msg = ""

    kwargs: dict[str, Any] = dict(
        model=cfg.claude_model,
        max_tokens=8096,
        temperature=0.0,
        messages=user_messages,
    )
    if system_msg.strip():
        kwargs["system"] = system_msg.strip()

    response = client.messages.create(**kwargs)
    raw = response.content[0].text if response.content else ""
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def _run_gemini(messages: list[dict]) -> dict[str, Any]:
    try:
        import google.generativeai as genai
        import base64
    except ImportError as e:
        raise ImportError("google-generativeai not installed. Run: pip install google-generativeai") from e

    # Read env directly at call time — same as v1, avoids singleton timing issues
    api_key    = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY in environment")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    parts = []
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    parts.append(f"{role}:\n{block['text']}\n\n")
                elif block.get("type") == "image":
                    label = block.get("label", "")
                    if label:
                        parts.append(f"[Image: {label}]\n")
                    parts.append({
                        "mime_type": block.get("mime", "image/jpeg"),
                        "data": base64.standard_b64decode(block["b64"]),
                    })
        else:
            parts.append(f"{role}:\n{content}\n\n")

    response = model.generate_content(
        parts,
        generation_config={"temperature": 0.0, "response_mime_type": "application/json"},
        request_options={"timeout": 300},
    )
    return _parse_json(getattr(response, "text", "") or "")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _run_openai(messages: list[dict]) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError("openai not installed. Run: pip install openai") from e

    from config import llm_config
    cfg = llm_config()
    if not cfg.openai_api_key:
        raise ValueError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=cfg.openai_api_key, timeout=cfg.timeout_secs)
    oai_msgs = []

    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            oai_content = []
            for block in content:
                if block.get("type") == "text":
                    oai_content.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    if block.get("label"):
                        oai_content.append({"type": "text", "text": f"[{block['label']}]"})
                    oai_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.get('mime','image/jpeg')};base64,{block['b64']}", "detail": "low"},
                    })
            oai_msgs.append({"role": role, "content": oai_content})
        else:
            oai_msgs.append({"role": role, "content": content})

    response = client.chat.completions.create(
        model=cfg.openai_model,
        messages=oai_msgs,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return _parse_json(response.choices[0].message.content or "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "bedrock":   _run_bedrock,
    "claude":    _run_claude,
    "anthropic": _run_claude,
    "gemini":    _run_gemini,
    "openai":    _run_openai,
}


def run_llm(messages: list[dict], provider: str | None = None) -> dict[str, Any]:
    """
    Run an LLM call via the configured provider.

    Args:
        messages: OpenAI-style chat messages (with extended image blocks).
        provider: Override LLM_PROVIDER env var.

    Returns:
        Parsed JSON response dict.
    """
    provider = (provider or os.getenv("LLM_PROVIDER", "gemini")).lower()
    fn = _PROVIDERS.get(provider)
    if not fn:
        return {"error": f"Unknown provider: {provider}. Options: {list(_PROVIDERS)}"}

    has_content = any(
        (m.get("content") or "").strip() if isinstance(m.get("content"), str) else bool(m.get("content"))
        for m in messages
    )
    if not has_content:
        return {"error": "Empty prompt — no messages with content"}

    try:
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))
        return _retry(fn, messages, max_retries=max_retries)
    except ValueError as e:
        return {"error": f"Config error ({provider})", "details": str(e)}
    except Exception as e:
        logger.error("LLM call failed (%s): %s", provider, e, exc_info=True)
        return {"error": f"LLM call failed ({provider})", "details": str(e)}


def make_llm_fn(provider: str | None = None):
    """Return a bound callable suitable for passing to engine stages."""
    def _fn(messages: list[dict]) -> dict[str, Any]:
        return run_llm(messages, provider=provider)
    return _fn


def available_providers() -> list[str]:
    """Return providers that have credentials configured."""
    from config import llm_config
    cfg = llm_config()
    out = []
    # Bedrock uses IAM — always available if boto3 is installed and role is attached
    try:
        import boto3
        out.append("bedrock")
    except ImportError:
        pass
    if cfg.anthropic_api_key:
        out.append("claude")
    if cfg.gemini_api_key:
        out.append("gemini")
    if cfg.openai_api_key:
        out.append("openai")
    return out or ["bedrock"]


# v1 compatibility alias — runner.py and other callers use this name
run_validation = run_llm
get_available_providers = available_providers
