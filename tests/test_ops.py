import json
from pathlib import Path

from jobsearch_mcp_server.ops import run
from jobsearch_mcp_server.repository import SQLiteRepository


def _unknown_message(repository: SQLiteRepository) -> dict:
    message = repository.enqueue_outbox_message(
        "student@example.com",
        {"subject": "日报"},
        idempotency_key="operator-reconciliation",
        max_attempts=1,
    )
    claimed = repository.claim_outbox_message(worker_id="mailer")
    assert claimed is not None
    unknown = repository.mark_outbox_delivery_unknown(
        message["id"],
        claimed["claim_token"],
        error="SMTP acknowledgement was lost",
    )
    assert unknown is not None
    return unknown


def test_operator_can_list_and_confirm_ambiguous_delivery(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    repository = SQLiteRepository(data_dir)
    message = _unknown_message(repository)

    assert run(["--data-dir", str(data_dir), "outbox", "list-unknown"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == message["id"]
    assert "payload" not in listed[0]

    assert (
        run(
            [
                "--data-dir",
                str(data_dir),
                "outbox",
                "confirm-delivered",
                str(message["id"]),
            ]
        )
        == 0
    )
    assert repository.get_outbox_message(message["id"])["status"] == "sent"


def test_operator_retry_grants_one_controlled_attempt(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    repository = SQLiteRepository(data_dir)
    message = _unknown_message(repository)

    assert (
        run(
            [
                "--data-dir",
                str(data_dir),
                "outbox",
                "retry",
                str(message["id"]),
                "--delay-seconds",
                "0",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "retrying"
    assert result["max_attempts"] == 2
    assert repository.claim_outbox_message(worker_id="mailer-2") is not None
