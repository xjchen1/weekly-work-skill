#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_ISO_RANGE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})至(\d{4})-(\d{2})-(\d{2})$"
)
_SLASH_RANGE = re.compile(r"^(\d{2})/(\d{2})-(\d{2})/(\d{2})$")
_CHINESE_RANGE = re.compile(
    r"^(\d{1,2})月(\d{1,2})[日号](?:到|至)(?:(\d{1,2})月)?(\d{1,2})[日号]$"
)
_ISO_SINGLE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SLASH_SINGLE = re.compile(r"^(\d{2})/(\d{2})$")
_CHINESE_SINGLE = re.compile(r"^(\d{1,2})月(\d{1,2})[日号]$")


def _calendar_date(year: int, month: int, day: int, value: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("invalid date range: %s" % value) from exc


def _validated_range(
    start: date, end: date, value: str, years_are_explicit: bool
) -> Tuple[date, date]:
    if start <= end:
        return start, end
    if not years_are_explicit:
        raise ValueError("cross-year ranges require explicit years: %s" % value)
    raise ValueError("start date is after end date: %s" % value)


def parse_date_range(
    value: str, today: Optional[date] = None
) -> Tuple[date, date]:
    """Parse a supported single date or inclusive date range."""
    if not isinstance(value, str) or not value:
        raise ValueError("date range is required")
    reference = today or date.today()

    match = _ISO_RANGE.fullmatch(value)
    if match:
        parts = [int(part) for part in match.groups()]
        start = _calendar_date(parts[0], parts[1], parts[2], value)
        end = _calendar_date(parts[3], parts[4], parts[5], value)
        return _validated_range(start, end, value, True)

    match = _SLASH_RANGE.fullmatch(value)
    if match:
        start_month, start_day, end_month, end_day = (
            int(part) for part in match.groups()
        )
        start = _calendar_date(reference.year, start_month, start_day, value)
        end = _calendar_date(reference.year, end_month, end_day, value)
        return _validated_range(start, end, value, False)

    match = _CHINESE_RANGE.fullmatch(value)
    if match:
        start_month = int(match.group(1))
        start_day = int(match.group(2))
        end_month = int(match.group(3) or start_month)
        end_day = int(match.group(4))
        start = _calendar_date(reference.year, start_month, start_day, value)
        end = _calendar_date(reference.year, end_month, end_day, value)
        return _validated_range(start, end, value, False)

    match = _ISO_SINGLE.fullmatch(value)
    if match:
        parsed = _calendar_date(*(int(part) for part in match.groups()), value)
        return parsed, parsed

    match = _SLASH_SINGLE.fullmatch(value)
    if match:
        month, day = (int(part) for part in match.groups())
        parsed = _calendar_date(reference.year, month, day, value)
        return parsed, parsed

    match = _CHINESE_SINGLE.fullmatch(value)
    if match:
        month, day = (int(part) for part in match.groups())
        parsed = _calendar_date(reference.year, month, day, value)
        return parsed, parsed

    raise ValueError("invalid date range: %s" % value)


def resolve_project_root(cwd: Path) -> Path:
    """Return the Git root or the resolved cwd when no Git root exists."""
    candidate = Path(cwd)
    if not candidate.exists():
        raise FileNotFoundError("project path does not exist: %s" % candidate)
    if not candidate.is_dir():
        raise NotADirectoryError("project path is not a directory: %s" % candidate)
    resolved_cwd = candidate.resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(resolved_cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return resolved_cwd
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return resolved_cwd


def find_state_database(codex_home: Path) -> Path:
    """Return the first supported Codex state database path that exists."""
    home = Path(codex_home)
    candidates = (home / "state_5.sqlite", home / "sqlite" / "state_5.sqlite")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Codex state database not found; checked %s"
        % ", ".join(str(candidate) for candidate in candidates)
    )


def find_thread_rollout_path(database_path: Path, thread_id: str) -> Path:
    """Return the rollout JSONL path for a thread from the local Codex index."""
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None or not row[0]:
        raise FileNotFoundError("rollout path not found for thread: %s" % thread_id)
    rollout_path = Path(row[0]).expanduser().resolve()
    if not rollout_path.is_file():
        raise FileNotFoundError("rollout path does not exist: %s" % rollout_path)
    return rollout_path


def discover_project_threads(
    database_path: Path,
    project_root: Path,
    start: date,
    end: date,
    timezone_name: str = "Asia/Shanghai",
) -> List[dict]:
    """Return user-owned project threads overlapping an inclusive date range."""
    if start > end:
        raise ValueError("start date is after end date")
    local_timezone = ZoneInfo(timezone_name)
    start_epoch = int(
        datetime(start.year, start.month, start.day, tzinfo=local_timezone).timestamp()
    )
    try:
        day_after_end = end + timedelta(days=1)
    except OverflowError as exc:
        raise ValueError(
            "end date has no following day: %s" % end.isoformat()
        ) from exc
    end_epoch = int(
        datetime(
            day_after_end.year,
            day_after_end.month,
            day_after_end.day,
            tzinfo=local_timezone,
        ).timestamp()
    )
    resolved_project_root = Path(project_root).resolve()

    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """
            SELECT id, created_at, updated_at, archived, cwd
            FROM threads
            WHERE COALESCE(agent_role, '') = ''
              AND created_at < ?
              AND updated_at >= ?
            ORDER BY created_at, id
            """,
            (end_epoch, start_epoch),
        ).fetchall()
    finally:
        connection.close()

    threads = []
    for row in rows:
        try:
            Path(row[4]).resolve().relative_to(resolved_project_root)
        except ValueError:
            continue
        threads.append({
            "id": row[0],
            "created_at": row[1],
            "updated_at": row[2],
            "archived": bool(row[3]),
        })
    return threads


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    chunks = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"])
        elif isinstance(item, str):
            chunks.append(item)
    return "\n".join(chunk for chunk in chunks if chunk)


def _payload_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _item_from_record(record: dict) -> Optional[dict]:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")

    if payload_type == "message":
        text = _text_from_content(payload.get("content"))
        if not text:
            return None
        return {
            "role": payload.get("role") or "unknown",
            "type": "message",
            "text": text,
        }

    if payload_type == "function_call_output":
        text = _payload_text(payload.get("output"))
        if not text:
            return None
        return {
            "role": "tool",
            "type": "tool_output",
            "text": text,
        }

    if payload_type == "function_call":
        name = payload.get("name") or "tool"
        arguments = _payload_text(payload.get("arguments"))
        text = "%s %s" % (name, arguments) if arguments else str(name)
        return {
            "role": "tool",
            "type": "tool_call",
            "text": text,
        }

    return None


def export_thread_messages(
    database_path: Path,
    thread_id: str,
    start: date,
    end: date,
    timezone_name: str = "Asia/Shanghai",
    max_chars: int = 5000,
) -> dict:
    """Export local thread messages for an inclusive local-date range."""
    if start > end:
        raise ValueError("start date is after end date")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    local_timezone = ZoneInfo(timezone_name)
    rollout_path = find_thread_rollout_path(database_path, thread_id)

    items = []
    with rollout_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "invalid rollout JSON at line %d: %s" % (line_number, rollout_path)
                ) from exc
            timestamp = record.get("timestamp")
            parsed_timestamp = _parse_timestamp(timestamp)
            local_date = parsed_timestamp.astimezone(local_timezone).date()
            if local_date < start or local_date > end:
                continue
            item = _item_from_record(record)
            if item is None:
                continue
            items.append(
                {
                    "timestamp": timestamp,
                    "local_date": local_date.isoformat(),
                    "role": item["role"],
                    "type": item["type"],
                    "text": item["text"][:max_chars],
                }
            )
    return {"thread_id": thread_id, "items": items}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover Codex project threads")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--cwd", required=True, type=Path)
    discover.add_argument("--range", required=True, dest="date_range")
    discover.add_argument("--today", required=True)
    discover.add_argument("--codex-home", required=True, type=Path)
    discover.add_argument("--timezone", required=True)
    export = commands.add_parser("export")
    export.add_argument("--thread-id", required=True)
    export.add_argument("--start", required=True)
    export.add_argument("--end", required=True)
    export.add_argument("--codex-home", required=True, type=Path)
    export.add_argument("--timezone", required=True)
    export.add_argument("--max-chars", required=True, type=int)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        database = find_state_database(arguments.codex_home)
        if arguments.command == "discover":
            try:
                today = date.fromisoformat(arguments.today)
            except ValueError as exc:
                raise ValueError("invalid today date: %s" % arguments.today) from exc
            start, end = parse_date_range(arguments.date_range, today)
            project_root = resolve_project_root(arguments.cwd)
            threads = discover_project_threads(
                database,
                project_root,
                start,
                end,
                arguments.timezone,
            )
            payload = {
                "project_root": str(project_root),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "threads": threads,
            }
        elif arguments.command == "export":
            try:
                start = date.fromisoformat(arguments.start)
                end = date.fromisoformat(arguments.end)
            except ValueError as exc:
                raise ValueError(
                    "invalid export date range: %s to %s"
                    % (arguments.start, arguments.end)
                ) from exc
            payload = export_thread_messages(
                database,
                arguments.thread_id,
                start,
                end,
                arguments.timezone,
                arguments.max_chars,
            )
        else:
            raise ValueError("unknown command: %s" % arguments.command)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, sqlite3.Error, ZoneInfoNotFoundError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
