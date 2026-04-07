#!/usr/bin/env python
"""
FAST Batch Process — AI Assisted Forms Testing (CLI / IDE mode)
================================================================

Usage:
    python run_batch.py
    python run_batch.py --manifest path/to/manifest.yaml
    python run_batch.py --manifest manifest.yaml --output ./my-reports

Steps:
    1. Copy .env.example to .env and add your LLM API key
    2. Drop your PDF forms in forms/current/ and forms/benchmark/
    3. Edit manifest.yaml to define your test cases
    4. Run this file (right-click → Run in PyCharm, or python run_batch.py)
    5. Open the HTML reports generated in reports/
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── load .env from the batch_process directory ────────────────────────────────
_HERE = Path(__file__).resolve().parent
_dotenv_path = _HERE / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(_dotenv_path))
except ImportError:
    # dotenv not installed — environment vars must be set manually
    pass

# ── import batch modules ──────────────────────────────────────────────────────
sys.path.insert(0, str(_HERE))

from batch.manifest_loader import load_manifest      # noqa: E402
from batch.runner import run_all                     # noqa: E402
from batch.console import print_header, print_summary_table  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FAST Batch Process — AI form validation runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_batch.py
  python run_batch.py --manifest my_tests.yaml
  python run_batch.py --manifest manifest.yaml --output /tmp/fast-reports
        """,
    )
    parser.add_argument(
        "--manifest",
        default=str(_HERE / "manifest.yaml"),
        metavar="PATH",
        help="Path to manifest.yaml (default: manifest.yaml in this directory)",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help="Override the output directory for HTML reports",
    )
    args = parser.parse_args()

    print_header("FAST — AI Assisted Forms Testing  |  Batch Mode")

    # ── load manifest ─────────────────────────────────────────────────────────
    try:
        config = load_manifest(args.manifest)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n  ERROR loading manifest: {exc}\n", file=sys.stderr)
        return 1

    if args.output:
        config.output_dir = os.path.abspath(args.output)

    os.makedirs(config.output_dir, exist_ok=True)

    print(f"  Project    : {config.project_name}")
    if config.environment:
        print(f"  Environment: {config.environment}")
    if config.account:
        print(f"  Account    : {config.account}")
    print(f"  Tests      : {len(config.tests)}")
    print(f"  Provider   : {config.llm_provider}")
    print(f"  Reports    : {config.output_dir}")
    print()

    # ── run all tests ─────────────────────────────────────────────────────────
    results = run_all(config)

    # ── final summary ─────────────────────────────────────────────────────────
    print_summary_table(results)

    # ── exit code ─────────────────────────────────────────────────────────────
    if config.fail_on_critical:
        has_critical = any(r["status"] in ("CRITICAL", "FAIL", "ERROR") for r in results)
        return 1 if has_critical else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
