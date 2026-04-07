"""Terminal output helpers for FAST batch runner (no external dependencies)."""
from __future__ import annotations

_RESET = "\033[0m"
_BOLD  = "\033[1m"
_RED   = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN  = "\033[96m"
_DIM   = "\033[2m"


def _c(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}"


def print_header(text: str) -> None:
    line = "─" * 60
    print(f"\n{_c(line, _CYAN)}")
    print(f"  {_c(text, _BOLD + _CYAN)}")
    print(f"{_c(line, _CYAN)}\n")


def print_section(text: str) -> None:
    print(f"\n{_c('▸ ' + text, _BOLD)}")


def print_running(name: str, index: int, total: int) -> None:
    print(f"  {_c(f'[{index}/{total}]', _DIM)} Running: {_c(name, _BOLD)} ...", flush=True)


def print_result(name: str, status: str, errors: int, warnings: int, report_path: str) -> None:
    if status == "FAIL":
        badge = _c("✗ FAIL   ", _RED + _BOLD)
    elif status == "REVIEW":
        badge = _c("⚠ REVIEW ", _YELLOW + _BOLD)
    else:
        badge = _c("✓ PASS   ", _GREEN + _BOLD)

    counts = _c(f"{errors}E {warnings}W", _DIM)
    print(f"  {badge}  {name:<45} {counts}")
    print(f"           {_c('Report → ' + report_path, _DIM)}")


def print_error(name: str, message: str) -> None:
    print(f"  {_c('✗ ERROR  ', _RED + _BOLD)}  {name}")
    print(f"           {_c(message, _RED)}")


def print_summary_table(results: list[dict]) -> None:
    total   = len(results)
    passed  = sum(1 for r in results if r["status"] == "PASS")
    reviews = sum(1 for r in results if r["status"] == "REVIEW")
    failed  = sum(1 for r in results if r["status"] == "FAIL")
    errors  = sum(1 for r in results if r["status"] == "ERROR")

    line = "─" * 60
    print(f"\n{_c(line, _CYAN)}")
    print(f"  {_c('FAST Batch — Summary', _BOLD)}")
    print(f"{_c(line, _CYAN)}")
    print(f"  Total tests : {total}")
    print(f"  {_c('PASS   : ' + str(passed), _GREEN)}")
    if reviews:
        print(f"  {_c('REVIEW : ' + str(reviews), _YELLOW)}")
    if failed:
        print(f"  {_c('FAIL   : ' + str(failed), _RED)}")
    if errors:
        print(f"  {_c('ERROR  : ' + str(errors), _RED)}")
    print(f"{_c(line, _CYAN)}\n")
