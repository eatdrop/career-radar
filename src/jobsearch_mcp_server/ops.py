"""Trusted local operator commands for durable queue reconciliation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .repository import SQLiteRepository


def _parser(default_data_dir: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="智职引擎本地运维工具")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir,
        help="服务使用的数据目录",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    outbox = commands.add_parser("outbox", help="检查和处置邮件 Outbox")
    actions = outbox.add_subparsers(dest="action", required=True)

    actions.add_parser("list-unknown", help="列出送达状态不明确的邮件")

    delivered = actions.add_parser("confirm-delivered", help="确认邮件已经送达")
    delivered.add_argument("message_id", type=int)
    delivered.add_argument("--note", default="operator confirmed delivery")

    retry = actions.add_parser("retry", help="确认未送达并安排一次受控重试")
    retry.add_argument("message_id", type=int)
    retry.add_argument("--delay-seconds", type=int, default=0)
    retry.add_argument("--note", default="operator confirmed no delivery")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    defaults = Settings.from_env()
    args = _parser(defaults.data_dir).parse_args(argv)
    repository = SQLiteRepository(args.data_dir.resolve())

    if args.action == "list-unknown":
        messages = repository.list_outbox_messages(status="delivery_unknown")
        safe_rows = [
            {
                "id": item["id"],
                "status": item["status"],
                "recipient": item["recipient"],
                "attempts": item["attempts"],
                "max_attempts": item["max_attempts"],
                "last_error": item["last_error"],
                "updated_at": item["updated_at"],
            }
            for item in messages
        ]
        print(json.dumps(safe_rows, ensure_ascii=False, indent=2))
        return 0

    if args.message_id < 1:
        raise SystemExit("message_id 必须是正整数")
    if args.action == "confirm-delivered":
        resolved = repository.resolve_outbox_delivery_unknown(
            args.message_id,
            delivered=True,
            note=args.note,
        )
    else:
        if not 0 <= args.delay_seconds <= 86_400:
            raise SystemExit("delay-seconds 必须在 0–86400 之间")
        resolved = repository.resolve_outbox_delivery_unknown(
            args.message_id,
            delivered=False,
            available_at=datetime.now(UTC) + timedelta(seconds=args.delay_seconds),
            note=args.note,
        )
    if resolved is None:
        raise SystemExit("目标邮件不存在，或状态不是 delivery_unknown")
    print(
        json.dumps(
            {
                "id": resolved["id"],
                "status": resolved["status"],
                "attempts": resolved["attempts"],
                "max_attempts": resolved["max_attempts"],
                "available_at": resolved["available_at"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
