#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from typing import Optional

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


if __package__:
    from . import weekly_store
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import weekly_store


STATE_FILE_NAME = "reminders.json"
REMINDER_HOUR = 17


def _state_path(root: Path) -> Path:
    return Path(root) / "state" / STATE_FILE_NAME


def _backup_malformed(path: Path, content: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    counter = 0
    while True:
        suffix = "" if counter == 0 else "-%d" % counter
        backup = path.with_name(path.name + ".broken-" + stamp + suffix + ".bak")
        try:
            with backup.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return backup
        except FileExistsError:
            counter += 1


@contextmanager
def _exclusive_lock(root: Path):
    lock_path = Path(root) / "state" / "reminder.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        mechanism = _lock_file_exclusive(handle)
        try:
            yield
        finally:
            _unlock_file_exclusive(handle, mechanism)


def _lock_file_exclusive(handle):
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return "fcntl"
    if msvcrt is not None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return "msvcrt"
    raise OSError("no supported file locking module: fcntl or msvcrt")


def _unlock_file_exclusive(handle, mechanism: str) -> None:
    if mechanism == "fcntl":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if mechanism == "msvcrt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise OSError("unknown file locking mechanism: %s" % mechanism)


def _raise_invalid_state(path: Path, content: bytes, reason: str) -> None:
    backup = _backup_malformed(path, content)
    raise ValueError(
        "%s in %s; backup written to %s" % (reason, path, backup)
    )


def _read_state(path: Path) -> Optional[str]:
    if not path.exists():
        return None

    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _raise_invalid_state(path, content, "Malformed reminder state")
    if not isinstance(payload, dict):
        _raise_invalid_state(path, content, "Invalid reminder state")
    last_reminded = payload.get("last_reminded")
    if not isinstance(last_reminded, str):
        _raise_invalid_state(path, content, "Invalid reminder state")
    return last_reminded


def _write_state(path: Path, last_reminded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            {"last_reminded": last_reminded},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

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


def should_remind(root: Path, now: Optional[datetime] = None) -> bool:
    current = now or datetime.now()
    if current.hour < REMINDER_HOUR:
        return False

    with _exclusive_lock(root):
        if weekly_store.has_entries_for_date(root, current.date()):
            return False

        state_path = _state_path(root)
        last_reminded = _read_state(state_path)
        today = current.date().isoformat()
        if last_reminded == today:
            return False

        _write_state(state_path, today)
        return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check weekly work reminder state")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check")
    check.add_argument("--root", required=True, type=Path)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "check":
            result = should_remind(arguments.root)
            print("REMIND" if result else "SKIP")
            return 0
        raise ValueError("unknown command: %s" % arguments.command)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
