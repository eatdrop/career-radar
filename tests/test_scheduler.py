import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jobsearch_mcp_server.scheduler import RadarScheduler
from jobsearch_mcp_server.services import CareerService
from tests.helpers import SAMPLE_RESUME, make_settings


def test_scheduler_is_idempotent_per_day(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})
    service.update_radar_settings(
        {
            "enabled": True,
            "schedule_time": "21:00",
            "sources": ["demo"],
            "min_score": 0,
        }
    )
    scheduler = RadarScheduler(service, "Asia/Shanghai")
    timezone = ZoneInfo("Asia/Shanghai")
    before_schedule = datetime(2026, 7, 29, 20, 59, tzinfo=timezone)
    after_schedule = datetime(2026, 7, 29, 21, 37, tzinfo=timezone)

    assert scheduler.tick(before_schedule) is False
    assert scheduler.tick(after_schedule) is True
    assert scheduler.tick(after_schedule) is False
    jobs = service.repository.list_operation_jobs(job_type="radar_run")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"


def test_scheduler_hashes_timezone_names_with_header_unsafe_characters(
    tmp_path: Path,
) -> None:
    service = CareerService(make_settings(tmp_path))
    service.analyse_resume({"text": SAMPLE_RESUME})
    service.update_radar_settings(
        {
            "enabled": True,
            "schedule_time": "09:00",
            "sources": ["demo"],
            "min_score": 0,
        }
    )
    timezone_name = "Etc/GMT+8"
    scheduler = RadarScheduler(service, timezone_name)
    now = datetime(2026, 7, 29, 9, 1, tzinfo=ZoneInfo(timezone_name))

    assert scheduler.tick(now) is True
    assert scheduler.tick(now) is False
    jobs = service.repository.list_operation_jobs(job_type="radar_run")
    assert len(jobs) == 1
    assert "+" not in jobs[0]["idempotency_key"]


def test_scheduler_checks_immediately_when_started(tmp_path: Path) -> None:
    service = CareerService(make_settings(tmp_path))
    scheduler = RadarScheduler(service, "Asia/Shanghai")
    called = threading.Event()

    def immediate_tick(now: datetime | None = None) -> bool:
        called.set()
        return False

    scheduler.tick = immediate_tick  # type: ignore[method-assign]
    scheduler.start()
    try:
        assert called.wait(timeout=1)
    finally:
        scheduler.stop()
