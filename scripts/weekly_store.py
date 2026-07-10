#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from typing import List, Optional, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


SCHEMA_VERSION = 1
CATEGORIES = (
    "daily_work",
    "literature",
    "research_progress",
    "research_outputs",
    "unresolved",
)
TERMINAL_SENTENCE_PUNCTUATION = "。.!！?？；;"
IDENTIFIER_LIKE_FIELD_NAMES = {
    "doi",
    "url",
    "link",
    "path",
    "file",
    "location",
    "id",
}
IDENTIFIER_LIKE_CHINESE_TOKENS = ("链接", "路径", "文件", "位置", "doi")


def normalize_date(value: str, today: Optional[date] = None) -> date:
    if not isinstance(value, str) or not value:
        raise ValueError("date is required")

    try:
        if len(value) == 5 and value[2] == "/":
            reference = today or date.today()
            return datetime.strptime(
                "%04d/%s" % (reference.year, value), "%Y/%m/%d"
            ).date()
        if len(value) == 10:
            return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid date: %s" % value) from exc

    raise ValueError("invalid date: %s" % value)


def week_bounds(day: date) -> Tuple[date, date]:
    if not isinstance(day, date):
        raise ValueError("week date must be a date")
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _weekly_path(root: Path, day: date) -> Path:
    week_start, _ = week_bounds(day)
    return Path(root) / "data" / (week_start.isoformat() + ".json")


def _empty_document(day: date) -> dict:
    week_start, week_end = week_bounds(day)
    return {
        "schema_version": SCHEMA_VERSION,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "entries": [],
    }


def _backup_malformed(path: Path, content: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(path.name + ".broken-" + stamp + ".bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            path.name + ".broken-" + stamp + "-%d.bak" % counter
        )
        counter += 1

    with candidate.open("xb") as backup:
        backup.write(content)
        backup.flush()
        os.fsync(backup.fileno())
    return candidate


def _read_document(path: Path, day: date) -> dict:
    if not path.exists():
        return _empty_document(day)

    content = path.read_bytes()
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        backup = _backup_malformed(path, content)
        raise ValueError(
            "Malformed weekly JSON in %s; backup written to %s" % (path, backup)
        ) from exc

    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        backup = _backup_malformed(path, content)
        raise ValueError(
            "Invalid weekly document in %s; backup written to %s" % (path, backup)
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        backup = _backup_malformed(path, content)
        raise ValueError(
            "Unsupported schema version in %s; backup written to %s" % (path, backup)
        )
    return document


@contextmanager
def _storage_lock(root: Path):
    data_directory = Path(root) / "data"
    data_directory.mkdir(parents=True, exist_ok=True)
    lock_path = data_directory / ".weekly-store.lock"
    with lock_path.open("a+b") as lock_file:
        mechanism = _lock_file_exclusive(lock_file)
        try:
            yield
        finally:
            _unlock_file_exclusive(lock_file, mechanism)


def _lock_file_exclusive(lock_file):
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return "fcntl"
    if msvcrt is not None:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return "msvcrt"
    raise OSError("no supported file locking module: fcntl or msvcrt")


def _unlock_file_exclusive(lock_file, mechanism: str) -> None:
    if mechanism == "fcntl":
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if mechanism == "msvcrt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise OSError("unknown file locking mechanism: %s" % mechanism)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _write_document(path: Path, document: dict) -> None:
    content = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _write_bytes_atomic(path, content)


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _normalize_comparison_string(
    value: str, strip_terminal_punctuation: bool
) -> str:
    normalized = _collapse_whitespace(unicodedata.normalize("NFC", value))
    if not strip_terminal_punctuation:
        return normalized

    stripped = normalized.rstrip(TERMINAL_SENTENCE_PUNCTUATION)
    if stripped:
        return stripped
    return normalized


def _normalize_key_for_classification(key: str) -> str:
    normalized = unicodedata.normalize("NFC", key).strip()
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", normalized)
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    normalized = re.sub(r"[\s_\-/\\|,，、;；:：(){}\[\]]+", " ", normalized)
    return normalized.casefold()


def _is_identifier_like_field_name(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _normalize_key_for_classification(key)
    english_tokens = set(re.findall(r"[a-z]+", normalized))
    if english_tokens & IDENTIFIER_LIKE_FIELD_NAMES:
        return True
    if "citation" in english_tokens and "key" in english_tokens:
        return True
    return any(token in normalized for token in IDENTIFIER_LIKE_CHINESE_TOKENS)


def _normalize_category_value_for_fingerprint(
    value: object, strip_terminal_punctuation: bool = True
) -> object:
    if isinstance(value, str):
        return _normalize_comparison_string(value, strip_terminal_punctuation)
    if isinstance(value, list):
        return [
            _normalize_category_value_for_fingerprint(item, strip_terminal_punctuation)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _normalize_category_value_for_fingerprint(item, strip_terminal_punctuation)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _normalize_category_value_for_fingerprint(
                item,
                strip_terminal_punctuation=(
                    strip_terminal_punctuation
                    and not _is_identifier_like_field_name(key)
                ),
            )
            for key, item in value.items()
        }
    return value


def _canonical_payload(payload: dict, today: Optional[date]) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    work_date = normalize_date(payload.get("date"), today=today)
    project = payload.get("project", "")
    ai_source = payload.get("ai_source", "")
    if not isinstance(project, str):
        raise ValueError("project must be a string")
    if not isinstance(ai_source, str):
        raise ValueError("ai_source must be a string")

    canonical = {
        "date": work_date.isoformat(),
        "project": project,
        "ai_source": ai_source,
    }
    for category in CATEGORIES:
        canonical[category] = _as_list(payload.get(category))
    return canonical


def _comparison_payload(canonical: dict) -> dict:
    comparison = {
        "date": canonical["date"],
        "project": _normalize_comparison_string(
            canonical["project"], strip_terminal_punctuation=False
        ),
        "ai_source": _normalize_comparison_string(
            canonical["ai_source"], strip_terminal_punctuation=False
        ),
    }
    for category in CATEGORIES:
        comparison[category] = _normalize_category_value_for_fingerprint(
            canonical[category]
        )
    return comparison


def _fingerprint(canonical: dict) -> str:
    try:
        encoded = json.dumps(
            _comparison_payload(canonical),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload contains values that cannot be stored as JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _make_entry(
    payload: dict, today: Optional[date], collected_at: Optional[str] = None
) -> dict:
    canonical = _canonical_payload(payload, today)
    fingerprint = _fingerprint(canonical)
    entry = dict(canonical)
    entry.update(
        {
            "record_id": fingerprint[:16],
            "fingerprint": fingerprint,
            "collected_at": collected_at or _timestamp(),
        }
    )
    return entry


def _result(status: str, entry: dict) -> dict:
    return {
        "status": status,
        "record_id": entry["record_id"],
        "entry": entry,
    }


def add_entry(
    root: Path,
    payload: dict,
    today: Optional[date] = None,
    now: Optional[str] = None,
) -> dict:
    entry = _make_entry(payload, today=today, collected_at=now)
    work_date = normalize_date(entry["date"])
    path = _weekly_path(root, work_date)
    with _storage_lock(root):
        document = _read_document(path, work_date)

        for existing in document["entries"]:
            if existing.get("fingerprint") == entry["fingerprint"]:
                return _result("duplicate", existing)
            if existing.get("record_id") == entry["record_id"]:
                raise ValueError("record ID collision: %s" % entry["record_id"])

        document["entries"].append(entry)
        _write_document(path, document)
        return _result("added", entry)


def list_entries(root: Path, week_of: date) -> List[dict]:
    path = _weekly_path(root, week_of)
    if not path.exists():
        return []
    return list(_read_document(path, week_of)["entries"])


def has_entries_for_date(root: Path, day: date) -> bool:
    expected = day.isoformat()
    return any(entry.get("date") == expected for entry in list_entries(root, day))


def _weekly_files(data_directory: Path):
    for path in sorted(data_directory.glob("????-??-??.json")):
        try:
            week_start = normalize_date(path.stem)
        except ValueError:
            continue
        if path.name == week_start.isoformat() + ".json":
            yield path, week_start


def _find_entry(root: Path, record_id: str) -> Tuple[Path, dict, int]:
    matches = []
    data_directory = Path(root) / "data"
    if data_directory.exists():
        for path, week_start in _weekly_files(data_directory):
            document = _read_document(path, week_start)
            for index, entry in enumerate(document["entries"]):
                if entry.get("record_id") == record_id:
                    matches.append((path, document, index))

    if not matches:
        raise ValueError("record not found: %s" % record_id)
    if len(matches) > 1:
        raise ValueError("record ID is not unique: %s" % record_id)
    return matches[0]


def replace_entry(
    root: Path,
    record_id: str,
    payload: dict,
    today: Optional[date] = None,
) -> dict:
    replacement = _make_entry(payload, today=today)
    with _storage_lock(root):
        source_path, source_document, source_index = _find_entry(root, record_id)
        replacement_date = normalize_date(replacement["date"])
        destination_path = _weekly_path(root, replacement_date)

        if destination_path == source_path:
            for index, entry in enumerate(source_document["entries"]):
                if index != source_index and entry.get("record_id") == replacement["record_id"]:
                    raise ValueError("replacement duplicates an existing entry")
            source_document["entries"][source_index] = replacement
            _write_document(source_path, source_document)
        else:
            destination_document = _read_document(destination_path, replacement_date)
            if any(
                entry.get("record_id") == replacement["record_id"]
                for entry in destination_document["entries"]
            ):
                raise ValueError("replacement duplicates an existing entry")

            source_before = source_path.read_bytes()
            destination_existed = destination_path.exists()
            destination_before = (
                destination_path.read_bytes() if destination_existed else None
            )
            try:
                destination_document["entries"].append(replacement)
                _write_document(destination_path, destination_document)
                del source_document["entries"][source_index]
                _write_document(source_path, source_document)
            except Exception as operation_error:
                rollback_failures = []
                try:
                    _write_bytes_atomic(source_path, source_before)
                except Exception as rollback_error:
                    rollback_failures.append(("source", rollback_error))

                try:
                    if destination_existed:
                        _write_bytes_atomic(destination_path, destination_before)
                    else:
                        destination_path.unlink()
                except FileNotFoundError:
                    pass
                except Exception as rollback_error:
                    rollback_failures.append(("destination", rollback_error))

                if rollback_failures:
                    details = "; ".join(
                        "%s rollback: %s" % (label, error)
                        for label, error in rollback_failures
                    )
                    raise RuntimeError(
                        "cross-week replace failed: %s; rollback failures: %s"
                        % (operation_error, details)
                    ) from operation_error
                raise

        return _result("replaced", replacement)


def delete_entry(root: Path, record_id: str, week_of: date) -> bool:
    path = _weekly_path(root, week_of)
    with _storage_lock(root):
        if not path.exists():
            return False
        document = _read_document(path, week_of)
        for index, entry in enumerate(document["entries"]):
            if entry.get("record_id") == record_id:
                del document["entries"][index]
                _write_document(path, document)
                return True
        return False


def _read_stdin_object() -> dict:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError("stdin must contain exactly one JSON object: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise ValueError("stdin must contain exactly one JSON object")
    return payload


def _parse_cli_date(value: str, label: str) -> date:
    try:
        return normalize_date(value)
    except ValueError as exc:
        raise ValueError("invalid %s: %s" % (label, value)) from exc


def _add_common_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, type=Path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store weekly work entries")
    commands = parser.add_subparsers(dest="command", required=True)

    add_parser = commands.add_parser("add")
    _add_common_root(add_parser)
    add_parser.add_argument("--today")

    list_parser = commands.add_parser("list")
    _add_common_root(list_parser)
    list_parser.add_argument("--week-of", required=True)

    replace_parser = commands.add_parser("replace")
    _add_common_root(replace_parser)
    replace_parser.add_argument("record_id", nargs="?")
    replace_parser.add_argument("--record-id", dest="record_id_option")
    replace_parser.add_argument("--today")

    delete_parser = commands.add_parser("delete")
    _add_common_root(delete_parser)
    delete_parser.add_argument("record_id", nargs="?")
    delete_parser.add_argument("--record-id", dest="record_id_option")
    delete_parser.add_argument("--week-of", required=True)

    has_date_parser = commands.add_parser("has-date")
    _add_common_root(has_date_parser)
    has_date_parser.add_argument("--date", required=True)
    return parser


def _record_id(arguments: argparse.Namespace) -> str:
    positional = arguments.record_id
    optional = arguments.record_id_option
    if positional and optional:
        raise ValueError("provide record ID once")
    record_id = positional or optional
    if not record_id:
        raise ValueError("record ID is required")
    return record_id


def _dispatch(arguments: argparse.Namespace) -> object:
    if arguments.command == "add":
        today = _parse_cli_date(arguments.today, "today") if arguments.today else None
        return add_entry(arguments.root, _read_stdin_object(), today=today)
    if arguments.command == "list":
        week_of = _parse_cli_date(arguments.week_of, "week-of")
        return list_entries(arguments.root, week_of)
    if arguments.command == "replace":
        today = _parse_cli_date(arguments.today, "today") if arguments.today else None
        return replace_entry(
            arguments.root, _record_id(arguments), _read_stdin_object(), today=today
        )
    if arguments.command == "delete":
        week_of = _parse_cli_date(arguments.week_of, "week-of")
        return {
            "deleted": delete_entry(arguments.root, _record_id(arguments), week_of)
        }
    if arguments.command == "has-date":
        day = _parse_cli_date(arguments.date, "date")
        return {"has_entries": has_entries_for_date(arguments.root, day)}
    raise ValueError("unknown command: %s" % arguments.command)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        result = _dispatch(arguments)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
