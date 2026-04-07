"""Parse and validate manifest.yaml for the FAST batch runner."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


VALID_MODES = {"basic", "specific", "benchmark"}
VALID_PROVIDERS = {"gemini", "openai", "claude"}


@dataclass
class TestEntry:
    name: str
    current_pdf: str         # absolute path to the current/updated form
    benchmark_pdf: Optional[str]  # absolute path to the baseline form (or None)
    mode: str                # basic | specific | benchmark
    prompt: str              # optional validation rules / custom prompt


@dataclass
class BatchConfig:
    project_name: str
    environment: str
    account: str
    llm_provider: str
    output_dir: str          # absolute path where HTML reports are written
    fail_on_critical: bool
    tests: list[TestEntry] = field(default_factory=list)


def load_manifest(manifest_path: str) -> BatchConfig:
    """
    Load and validate a manifest.yaml file.

    Raises:
        FileNotFoundError: if manifest or a referenced PDF does not exist.
        ValueError: if required fields are missing or values are invalid.
    """
    manifest_path = os.path.abspath(manifest_path)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest_dir = os.path.dirname(manifest_path)
    forms_dir = os.path.join(manifest_dir, "forms")

    with open(manifest_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError("manifest.yaml must be a YAML mapping at the top level.")

    # ── project section ──────────────────────────────────────────────────────
    project = raw.get("project") or {}
    project_name = str(project.get("name") or "FAST Batch").strip()
    environment  = str(project.get("environment") or "").strip()
    account      = str(project.get("account") or "").strip()

    # ── settings section ─────────────────────────────────────────────────────
    settings = raw.get("settings") or {}

    default_mode     = str(settings.get("mode") or "basic").strip().lower()
    llm_provider     = str(settings.get("llm_provider") or "gemini").strip().lower()
    raw_output_dir   = str(settings.get("output_dir") or "./reports").strip()
    fail_on_critical = bool(settings.get("fail_on_critical", False))

    if default_mode not in VALID_MODES:
        raise ValueError(
            f"settings.mode '{default_mode}' is invalid. "
            f"Must be one of: {', '.join(sorted(VALID_MODES))}"
        )
    if llm_provider not in VALID_PROVIDERS:
        raise ValueError(
            f"settings.llm_provider '{llm_provider}' is invalid. "
            f"Must be one of: {', '.join(sorted(VALID_PROVIDERS))}"
        )

    # Resolve output_dir relative to the manifest location
    output_dir = os.path.normpath(os.path.join(manifest_dir, raw_output_dir))

    # ── tests section ────────────────────────────────────────────────────────
    raw_tests = raw.get("tests") or []
    if not raw_tests:
        raise ValueError("manifest.yaml has no tests defined under the 'tests' key.")

    tests: list[TestEntry] = []
    for i, t in enumerate(raw_tests):
        if not isinstance(t, dict):
            raise ValueError(f"tests[{i}] must be a mapping, got: {type(t).__name__}")

        name = str(t.get("name") or f"Test {i + 1}").strip()
        mode = str(t.get("mode") or default_mode).strip().lower()
        prompt = str(t.get("prompt") or "").strip()

        if mode not in VALID_MODES:
            raise ValueError(
                f"tests[{i}] ('{name}') has invalid mode '{mode}'. "
                f"Must be one of: {', '.join(sorted(VALID_MODES))}"
            )

        # ── current PDF ──────────────────────────────────────────────────────
        current_raw = t.get("current")
        if not current_raw:
            raise ValueError(f"tests[{i}] ('{name}') is missing required field 'current'.")
        current_pdf = _resolve_pdf(str(current_raw).strip(), forms_dir, name, "current")

        # ── benchmark PDF ────────────────────────────────────────────────────
        benchmark_pdf: Optional[str] = None
        bench_raw = t.get("benchmark")
        if mode == "benchmark":
            if not bench_raw:
                raise ValueError(
                    f"tests[{i}] ('{name}') uses mode 'benchmark' but is missing 'benchmark' PDF."
                )
            benchmark_pdf = _resolve_pdf(str(bench_raw).strip(), forms_dir, name, "benchmark")
        elif bench_raw:
            # benchmark PDF provided but mode is not benchmark — honour it anyway
            benchmark_pdf = _resolve_pdf(str(bench_raw).strip(), forms_dir, name, "benchmark")

        tests.append(TestEntry(
            name=name,
            current_pdf=current_pdf,
            benchmark_pdf=benchmark_pdf,
            mode=mode,
            prompt=prompt,
        ))

    return BatchConfig(
        project_name=project_name,
        environment=environment,
        account=account,
        llm_provider=llm_provider,
        output_dir=output_dir,
        fail_on_critical=fail_on_critical,
        tests=tests,
    )


def _resolve_pdf(raw: str, forms_dir: str, test_name: str, label: str) -> str:
    """
    Resolve a PDF path. Accepts:
      - Paths relative to the forms/ directory (e.g. "current/form.pdf")
      - Absolute paths
    """
    if os.path.isabs(raw):
        candidate = raw
    else:
        candidate = os.path.normpath(os.path.join(forms_dir, raw))

    if not os.path.exists(candidate):
        raise FileNotFoundError(
            f"Test '{test_name}' — {label} PDF not found: {candidate}\n"
            f"  (looked for '{raw}' relative to {forms_dir})"
        )
    return candidate
