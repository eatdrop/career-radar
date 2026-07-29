"""Single-process scheduler for the local daily job radar."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .services import AppError, CareerService

LOGGER = logging.getLogger("jobsearch.scheduler")


class RadarScheduler:
    """Runs at most once per local calendar day.

    The scheduler is intentionally single-process. For replicated deployments,
    disable it with ``SCHEDULER_ENABLED=false`` and call the radar endpoint from
    an external scheduler that provides distributed locking.
    """

    def __init__(self, service: CareerService, timezone_name: str):
        self.service = service
        self.timezone_name = timezone_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="career-radar-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("scheduler tick failed")
            if self._stop.wait(20):
                break

    def tick(self, now: datetime | None = None) -> bool:
        try:
            timezone = ZoneInfo(self.timezone_name)
        except Exception:
            timezone = ZoneInfo("UTC")
        current = now.astimezone(timezone) if now else datetime.now(timezone)
        settings = self.service.get_radar_settings()
        if not settings.get("enabled"):
            return False
        try:
            hour_text, minute_text = str(settings.get("schedule_time", "")).split(":", 1)
            scheduled = current.replace(
                hour=int(hour_text),
                minute=int(minute_text),
                second=0,
                microsecond=0,
            )
        except (TypeError, ValueError):
            LOGGER.warning("scheduler ignored invalid schedule_time")
            return False
        if current < scheduled:
            return False

        today = current.date().isoformat()
        last_attempt = self.service.repository.get_state("radar_last_scheduled_date", "")
        if last_attempt == today:
            return False
        # Claim before executing so a slow or failing provider does not cause a
        # retry storm every 20 seconds.
        self.service.repository.set_state("radar_last_scheduled_date", today)
        try:
            self.service.run_radar(trigger_type="scheduled")
            LOGGER.info("scheduled radar completed date=%s", today)
        except AppError as exc:
            LOGGER.warning("scheduled radar failed date=%s code=%s", today, exc.code)
            if exc.code == "radar_already_running":
                # A manual run won the in-process lock. Retry on the next tick
                # instead of silently losing today's scheduled run.
                self.service.repository.set_state("radar_last_scheduled_date", "")
                return False
        return True
