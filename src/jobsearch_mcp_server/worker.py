"""Durable background workers for operations and transactional email delivery.

The supervisor intentionally owns no business logic.  Operation handlers and
the outbox sender are injected by the application, while SQLite remains the
source of truth for claims, leases, retries, and terminal state.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .repository import SQLiteRepository

LOGGER = logging.getLogger(__name__)

OperationHandler = Callable[[dict[str, Any]], Any]
OutboxSender = Callable[[dict[str, Any]], None]

_RETRYABLE_CLIENT_STATUSES = frozenset({408, 409, 429})
_MAX_STORED_ERROR_LENGTH = 1_000


class PermanentWorkerError(Exception):
    """Signal that retrying an operation or message cannot make it succeed."""

    def __init__(self, message: str, *, code: str = "permanent_worker_error"):
        super().__init__(message)
        self.code = code


class DeliveryUnknownError(Exception):
    """Signal an ambiguous SMTP outcome that must not be retried automatically."""


class RetryWithoutAttemptError(Exception):
    """Defer known resource contention without spending retry budget."""

    def __init__(self, message: str, *, retry_after_seconds: float = 30.0):
        super().__init__(message)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))


def _is_permanent_error(error: BaseException) -> bool:
    if isinstance(error, PermanentWorkerError):
        return True
    # Import lazily so services may safely import the worker during app wiring.
    from .services import AppError

    return (
        isinstance(error, AppError)
        and 400 <= error.status < 500
        and error.status not in _RETRYABLE_CLIENT_STATUSES
    )


def _safe_error_text(error: BaseException) -> str:
    from .services import AppError

    if isinstance(error, AppError):
        text = error.message
    else:
        text = str(error).strip()
    if not text:
        text = type(error).__name__
    text = " ".join(text.replace("\x00", "").split())
    return text[:_MAX_STORED_ERROR_LENGTH]


def _stored_operation_error(error: BaseException) -> str:
    """Persist a stable, safe error envelope without changing the queue schema."""
    from .services import AppError

    message = _safe_error_text(error)
    if isinstance(error, AppError):
        code = error.code
        retryable = not _is_permanent_error(error)
    elif isinstance(error, PermanentWorkerError):
        code = error.code
        retryable = False
    else:
        code = "radar_run_failed"
        retryable = True
    return json.dumps(
        {"code": code, "message": message, "retryable": retryable},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _LeaseHeartbeat:
    """Renew one claim in the background while a callback is running."""

    def __init__(
        self,
        renew: Callable[..., dict[str, Any] | None],
        item_id: int,
        claim_token: str,
        lease_seconds: int,
    ):
        self._renew = renew
        self._item_id = item_id
        self._claim_token = claim_token
        self._lease_seconds = lease_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.claim_lost = False

    def __enter__(self) -> _LeaseHeartbeat:
        interval = max(0.1, min(30.0, self._lease_seconds / 3))
        self._thread = threading.Thread(
            target=self._run,
            args=(interval,),
            name=f"jobsearch-lease-{self._item_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            try:
                renewed = self._renew(
                    self._item_id,
                    self._claim_token,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as error:  # pragma: no cover - defensive DB outage path
                LOGGER.warning(
                    "lease renewal failed item_id=%s error_type=%s",
                    self._item_id,
                    type(error).__name__,
                )
                continue
            if renewed is None:
                self.claim_lost = True
                return


class DurableWorkerSupervisor:
    """Run one durable operation worker and one email-outbox worker.

    Handlers receive only the decoded operation payload.  The outbox callback
    receives the complete decoded message record so it can use its stable ID
    for an RFC Message-ID and its recipient/payload for delivery.
    """

    def __init__(
        self,
        repository: SQLiteRepository,
        operation_handlers: Mapping[str, OperationHandler],
        outbox_sender: OutboxSender,
        *,
        poll_interval: float = 1.0,
        lease_seconds: int = 300,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
        retry_jitter_ratio: float = 0.2,
        worker_id: str | None = None,
    ):
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError("retry_max_seconds cannot be smaller than retry_base_seconds")
        if not 0 <= retry_jitter_ratio <= 1:
            raise ValueError("retry_jitter_ratio must be between 0 and 1")
        if not callable(outbox_sender):
            raise TypeError("outbox_sender must be callable")

        handlers = dict(operation_handlers)
        if any(not isinstance(name, str) or not name.strip() for name in handlers):
            raise ValueError("operation handler names must be non-empty strings")
        if any(not callable(handler) for handler in handlers.values()):
            raise TypeError("operation handlers must be callable")

        generated_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        base_worker_id = (worker_id or generated_id).strip()
        if not base_worker_id:
            raise ValueError("worker_id cannot be empty")

        self.repository = repository
        self.operation_handlers = handlers
        self.outbox_sender = outbox_sender
        self.poll_interval = float(poll_interval)
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_max_seconds = float(retry_max_seconds)
        self.retry_jitter_ratio = float(retry_jitter_ratio)
        self.operation_worker_id = f"{base_worker_id}:operation"
        self.outbox_worker_id = f"{base_worker_id}:outbox"

        self._stop_event = threading.Event()
        self._operation_wake = threading.Event()
        self._outbox_wake = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return len(self._threads) == 2 and all(thread.is_alive() for thread in self._threads)

    def start(self) -> bool:
        """Start both workers; return ``False`` when already running."""
        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                return False
            self._stop_event.clear()
            self._operation_wake.clear()
            self._outbox_wake.clear()
            self._threads = [
                threading.Thread(
                    target=self._operation_loop,
                    name="jobsearch-operation-worker",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._outbox_loop,
                    name="jobsearch-outbox-worker",
                    daemon=True,
                ),
            ]
            for thread in self._threads:
                thread.start()
        self.wake()
        return True

    def stop(self, timeout: float | None = 10.0) -> bool:
        """Request graceful shutdown and wait for active callbacks to finish."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        self._stop_event.set()
        self.wake()
        with self._lifecycle_lock:
            threads = list(self._threads)
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        stopped = not any(thread.is_alive() for thread in threads)
        if stopped:
            with self._lifecycle_lock:
                if self._threads == threads:
                    self._threads = []
        return stopped

    def wake(self) -> None:
        """Wake both polling loops after new durable work is committed."""
        self._operation_wake.set()
        self._outbox_wake.set()

    def _operation_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = False
            try:
                processed = self._process_one_operation()
            except Exception as error:  # pragma: no cover - defensive DB outage path
                LOGGER.warning(
                    "operation worker iteration failed error_type=%s",
                    type(error).__name__,
                )
            if not processed:
                self._wait(self._operation_wake)

    def _outbox_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = False
            try:
                processed = self._process_one_outbox_message()
            except Exception as error:  # pragma: no cover - defensive DB outage path
                LOGGER.warning(
                    "outbox worker iteration failed error_type=%s",
                    type(error).__name__,
                )
            if not processed:
                self._wait(self._outbox_wake)

    def _wait(self, wake_event: threading.Event) -> None:
        wake_event.wait(self.poll_interval)
        wake_event.clear()

    def _process_one_operation(self) -> bool:
        job = self.repository.claim_operation_job(
            worker_id=self.operation_worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False

        job_id = int(job["id"])
        claim_token = str(job["claim_token"])
        handler = self.operation_handlers.get(str(job["job_type"]))
        if handler is None:
            self.repository.fail_operation_job(
                job_id,
                claim_token,
                error=f"unsupported operation type: {job['job_type']}",
            )
            return True

        heartbeat = _LeaseHeartbeat(
            self.repository.renew_operation_job,
            job_id,
            claim_token,
            self.lease_seconds,
        )
        handler_succeeded = False
        try:
            with heartbeat:
                result = handler(job["payload"])
                handler_succeeded = True
                if not heartbeat.claim_lost:
                    self.repository.complete_operation_job(job_id, claim_token, result)
        except RetryWithoutAttemptError as error:
            self.repository.defer_operation_job(
                job_id,
                claim_token,
                error=_safe_error_text(error),
                available_at=datetime.now(UTC) + timedelta(seconds=error.retry_after_seconds),
            )
        except Exception as error:
            if handler_succeeded:
                LOGGER.warning(
                    "operation result persisted but queue completion failed "
                    "job_id=%s error_type=%s",
                    job_id,
                    type(error).__name__,
                )
            else:
                self._handle_operation_error(job, claim_token, error)
        return True

    def _handle_operation_error(
        self,
        job: dict[str, Any],
        claim_token: str,
        error: BaseException,
    ) -> None:
        error_text = _stored_operation_error(error)
        LOGGER.warning(
            "operation failed job_id=%s job_type=%s attempt=%s error_type=%s",
            job["id"],
            job["job_type"],
            job["attempts"],
            type(error).__name__,
        )
        if _is_permanent_error(error):
            self.repository.fail_operation_job(
                int(job["id"]),
                claim_token,
                error=error_text,
            )
            return
        self.repository.retry_operation_job(
            int(job["id"]),
            claim_token,
            error=error_text,
            available_at=self._retry_at(int(job["attempts"])),
        )

    def _process_one_outbox_message(self) -> bool:
        message = self.repository.claim_outbox_message(
            worker_id=self.outbox_worker_id,
            lease_seconds=self.lease_seconds,
        )
        if message is None:
            return False

        message_id = int(message["id"])
        claim_token = str(message["claim_token"])
        heartbeat = _LeaseHeartbeat(
            self.repository.renew_outbox_message,
            message_id,
            claim_token,
            self.lease_seconds,
        )
        delivery_succeeded = False
        try:
            with heartbeat:
                self.outbox_sender(message)
                delivery_succeeded = True
                if not heartbeat.claim_lost:
                    self.repository.complete_outbox_message(message_id, claim_token)
        except DeliveryUnknownError as error:
            self._quarantine_delivery_unknown(message_id, claim_token, error)
        except Exception as error:
            if delivery_succeeded:
                self._quarantine_delivery_unknown(message_id, claim_token, error)
            else:
                self._handle_outbox_error(message, claim_token, error)
        return True

    def _quarantine_delivery_unknown(
        self,
        message_id: int,
        claim_token: str,
        error: BaseException,
    ) -> None:
        marker = getattr(self.repository, "mark_outbox_delivery_unknown", None)
        if marker is None:  # pragma: no cover - compatibility with custom repositories
            LOGGER.error(
                "delivery outcome is unknown but repository cannot quarantine message_id=%s",
                message_id,
            )
            return
        try:
            marker(
                message_id,
                claim_token,
                error=_safe_error_text(error),
            )
        except Exception as marker_error:  # pragma: no cover - defensive DB outage path
            # Leave the row in `sending`; stale-lease reconciliation will quarantine it.
            LOGGER.error(
                "delivery outcome quarantine failed message_id=%s error_type=%s",
                message_id,
                type(marker_error).__name__,
            )

    def _handle_outbox_error(
        self,
        message: dict[str, Any],
        claim_token: str,
        error: BaseException,
    ) -> None:
        error_text = _safe_error_text(error)
        LOGGER.warning(
            "outbox delivery failed message_id=%s attempt=%s error_type=%s",
            message["id"],
            message["attempts"],
            type(error).__name__,
        )
        if _is_permanent_error(error):
            self.repository.fail_outbox_message(
                int(message["id"]),
                claim_token,
                error=error_text,
            )
            return
        self.repository.retry_outbox_message(
            int(message["id"]),
            claim_token,
            error=error_text,
            available_at=self._retry_at(int(message["attempts"])),
        )

    def _retry_at(self, attempts: int) -> datetime:
        exponent = max(0, attempts - 1)
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2**exponent),
        )
        if delay and self.retry_jitter_ratio:
            delay *= random.uniform(
                1 - self.retry_jitter_ratio,
                1 + self.retry_jitter_ratio,
            )
        return datetime.now(UTC) + timedelta(seconds=max(0.0, delay))


__all__ = [
    "DeliveryUnknownError",
    "DurableWorkerSupervisor",
    "OperationHandler",
    "OutboxSender",
    "PermanentWorkerError",
    "RetryWithoutAttemptError",
]
