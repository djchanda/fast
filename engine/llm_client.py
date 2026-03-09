import os
import json
import re
from typing import Any, Dict

from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY in environment (.env)")

    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel(model_name)
    return _model


def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    text = text.strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """
    Try to recover the first top-level JSON object.
    """
    if not text:
        return text

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def _safe_json_loads(raw: str) -> Dict[str, Any]:
    raw = _strip_code_fences(raw)
    try:
        return json.loads(raw)
    except Exception:
        candidate = _extract_json_object(raw)
        return json.loads(candidate)


def run_validation(messages):
    """
    Run Gemini validation.

    Accepts: messages (list[dict]) in OpenAI-style chat format.
    Returns: dict (parsed JSON from Gemini) or error payload.
    """
    prompt_text = ""
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        prompt_text += f"{role}:\n{content}\n\n"

    if not prompt_text.strip():
        return {
            "error": "Validation aborted",
            "details": "Prompt was empty. No content was provided to the LLM.",
        }

    try:
        model = _get_model()
        response = model.generate_content(
            prompt_text,
            generation_config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )

        raw = getattr(response, "text", "") or ""
        raw = _strip_code_fences(raw)

        if not raw.strip():
            return {"error": "Gemini returned empty response", "details": "No JSON received."}

        return _safe_json_loads(raw)

    except Exception as e:
        return {"error": "Gemini request failed", "details": str(e)}