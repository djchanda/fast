from pathlib import Path
from flask import current_app

def get_html_report_dir() -> Path:
    # instance/reports is perfect (not tracked in git)
    base = Path(current_app.instance_path) / "reports"
    base.mkdir(parents=True, exist_ok=True)
    return base
