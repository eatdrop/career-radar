"""Single-process scheduler for the local daily job radar."""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .services import AppError, CareerService

LOGGER = logging.getLogger("jobsearch.scheduler")


class RadarScheduler:
    """Durably enqueue at most one radar operation per local calendar day."""

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

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

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
        timezone_token = hashlib.sha256(self.timezone_name.encode("utf-8")).hexdigest()[:16]
        idempotency_key = f"scheduled:{timezone_token}:{today}"
        try:
            operation = self.service.enqueue_radar_operation(
                {},
                idempotency_key=idempotency_key,
                trigger_type="scheduled",
            )
            if operation["deduplicated"]:
                return False
            LOGGER.info(
                "scheduled radar enqueued date=%s operation_id=%s",
                today,
                operation["id"],
            )
        except AppError as exc:
            LOGGER.warning("scheduled radar enqueue failed date=%s code=%s", today, exc.code)
            return False
        return True
