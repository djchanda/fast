"""
Scheduler service for cron-based automatic test runs.
Uses APScheduler if installed, falls back to a simple interval check.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_scheduler = None


def _parse_cron_next(cron_expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a cron expression and return the next run datetime."""
    try:
        from croniter import croniter
        base = after or datetime.utcnow()
        it = croniter(cron_expr, base)
        return it.get_next(datetime)
    except ImportError:
        # croniter not installed — schedule 24h from now
        from datetime import timedelta
        return (after or datetime.utcnow()) + timedelta(hours=24)
    except Exception as exc:
        logger.warning("Invalid cron expression '%s': %s", cron_expr, exc)
        return None


def init_scheduler(app):
    """Initialize APScheduler and register the cron job checker."""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            func=lambda: _check_due_schedules(app),
            trigger="interval",
            minutes=1,
            id="fast_scheduler",
            replace_existing=True,
        )
        _scheduler.start()
        logger.info("FAST scheduler started.")
    except ImportError:
        logger.info("APScheduler not installed — scheduled runs disabled.")


def _check_due_schedules(app):
    """Check for any scheduled runs that are due and execute them."""
    with app.app_context():
        try:
            from app.models.scheduled_run import ScheduledRun
            from app.models.test_case import TestCase
            from app.models.run import Run
            from app.models.run_result import RunResult
            from app.extensions import db
            from app.services.runner import run_testcase
            import json

            now = datetime.utcnow()
            due = ScheduledRun.query.filter(
                ScheduledRun.is_active == True,
                ScheduledRun.next_run_at <= now,
            ).all()

            for schedule in due:
                logger.info("Executing scheduled run '%s' (project %s)", schedule.name, schedule.project_id)
                tc_ids = schedule.testcase_id_list()
                test_cases = TestCase.query.filter(
                    TestCase.project_id == schedule.project_id,
                    TestCase.id.in_(tc_ids)
                ).all()

                if not test_cases:
                    logger.warning("Scheduled run %s has no valid test cases.", schedule.id)
                    continue

                run = Run(
                    project_id=schedule.project_id,
                    triggered_by=f"scheduler:{schedule.name}",
                    total=len(test_cases),
                )
                db.session.add(run)
                db.session.commit()

                for tc in test_cases:
                    rr = RunResult(
                        run_id=run.id,
                        project_id=schedule.project_id,
                        test_case_id=tc.id,
                        form_id=tc.form_id,
                        mode=tc.mode,
                        status="running",
                    )
                    db.session.add(rr)
                    db.session.commit()

                    try:
                        out = run_testcase(project_id=schedule.project_id, tc=tc, run_id=run.id, rr_id=rr.id)
                        rr.result_json = json.dumps(out.get("result_json") or {}, ensure_ascii=False, indent=2)
                        rr.summary_text = out.get("summary_text") or ""
                        rr.errors = int(out.get("errors") or 0)
                        rr.warnings = int(out.get("warnings") or 0)
                        rr.passed = int(out.get("passed") or 0)
                        rr.status = "completed"
                    except Exception as e:
                        rr.status = "failed"
                        rr.error_message = str(e)
                        rr.errors = 1

                    db.session.add(rr)
                    db.session.commit()

                run.status = "completed"
                db.session.commit()

                # Update schedule
                schedule.last_run_at = now
                schedule.last_run_id = run.id
                schedule.next_run_at = _parse_cron_next(schedule.cron_expression, after=now)
                db.session.commit()

                logger.info("Scheduled run '%s' complete — run #%s", schedule.name, run.id)

        except Exception as exc:
            logger.error("Scheduler check failed: %s", exc)


def compute_next_run(cron_expr: str) -> Optional[datetime]:
    return _parse_cron_next(cron_expr)
