"""Central configuration — all tuneable knobs in one place."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------
LLMProvider = Literal["claude", "gemini", "openai", "bedrock"]


@dataclass
class LLMConfig:
    provider: LLMProvider = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "claude").lower()  # type: ignore[return-value]
    )
    # Anthropic direct
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    # AWS Bedrock
    bedrock_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "ca-central-1"))
    bedrock_model_id: str = field(
        default_factory=lambda: os.getenv(
            "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"
        )
    )
    # Gemini
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    # OpenAI
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o"))

    timeout_secs: int = 300
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Document parser
# ---------------------------------------------------------------------------
ParserBackend = Literal["llamaparse", "pymupdf_vision", "pdfplumber"]


@dataclass
class ParserConfig:
    # Primary: LlamaParse cloud (best accuracy, handles OCR natively)
    llamaparse_api_key: str = field(default_factory=lambda: os.getenv("LLAMA_CLOUD_API_KEY", ""))
    llamaparse_result_type: str = "markdown"        # "markdown" | "text"
    llamaparse_premium_mode: bool = True            # enables AI-powered table + OCR
    llamaparse_language: str = "en"
    llamaparse_max_pages: int = 0                   # 0 = no limit

    # OCR thresholds
    scanned_word_threshold: int = 30               # avg words/page below this = scanned
    ocr_dpi: int = 300

    # Fallback chain when LlamaParse key is absent
    fallback_backend: ParserBackend = "pymupdf_vision"

    # Vision-LLM page rendering for fallback
    render_dpi: int = 150                          # for page-image pipeline
    render_fmt: str = "JPEG"
    render_quality: int = 85


# ---------------------------------------------------------------------------
# Semantic diff
# ---------------------------------------------------------------------------
@dataclass
class DiffConfig:
    # Similarity below this → page flagged as changed
    similarity_threshold: float = 0.9985
    # Pixel diff % above this → major change
    major_diff_pct: float = 2.0
    # Max tokens to send per document in semantic diff prompt
    max_doc_tokens: int = 12_000
    # Pages to send as images in vision benchmark
    max_image_pages: int = 40


# ---------------------------------------------------------------------------
# Singleton accessors (lazy, reads env at call time)
# ---------------------------------------------------------------------------
_llm: LLMConfig | None = None
_parser: ParserConfig | None = None
_diff: DiffConfig | None = None


def llm_config() -> LLMConfig:
    global _llm
    if _llm is None:
        _llm = LLMConfig()
    return _llm


def parser_config() -> ParserConfig:
    global _parser
    if _parser is None:
        _parser = ParserConfig()
    return _parser


def diff_config() -> DiffConfig:
    global _diff
    if _diff is None:
        _diff = DiffConfig()
    return _diff
