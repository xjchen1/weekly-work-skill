import importlib.util
import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts import reminder, weekly_store


class FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.calls = []

    def locking(self, fileno, mode, nbytes):
        self.calls.append((mode, nbytes))


def import_reminder_without_fcntl(fake_msvcrt):
    spec = importlib.util.spec_from_file_location(
        "reminder_no_fcntl",
        SKILL_ROOT / "scripts" / "reminder.py",
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": fake_msvcrt}):
        spec.loader.exec_module(module)
    return module


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_before_1659_returns_false_and_does_not_create_state(self):
        self.assertFalse(
            reminder.should_remind(
                self.root, now=datetime(2026, 7, 1, 16, 59)
            )
        )
        self.assertFalse((self.root / "state").exists())

    def test_exactly_1700_true_once_then_false_same_day_and_true_next_day(self):
        first = reminder.should_remind(self.root, now=datetime(2026, 7, 1, 17, 0))
        second = reminder.should_remind(self.root, now=datetime(2026, 7, 1, 18, 30))
        third = reminder.should_remind(self.root, now=datetime(2026, 7, 2, 17, 0))

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(third)

        state = json.loads((self.root / "state" / "reminders.json").read_text())
        self.assertEqual(state, {"last_reminded": "2026-07-02"})

    def test_same_day_work_entry_suppresses_reminder(self):
        weekly_store.add_entry(
            self.root,
            {
                "date": "2026-07-01",
                "project": "alpha",
                "ai_source": "codex",
                "daily_work": ["Implemented reminder"],
                "literature": [],
                "research_progress": [],
                "research_outputs": [],
                "unresolved": [],
            },
        )

        self.assertFalse(
            reminder.should_remind(self.root, now=datetime(2026, 7, 1, 17, 0))
        )

    def test_reminder_lock_falls_back_to_windows_msvcrt_when_fcntl_is_unavailable(self):
        fake_msvcrt = FakeMsvcrt()
        module = import_reminder_without_fcntl(fake_msvcrt)

        with module._exclusive_lock(self.root):
            pass

        self.assertEqual(
            fake_msvcrt.calls,
            [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)],
        )

    def test_before_17_does_not_create_state_directory(self):
        reminder.should_remind(self.root, now=datetime(2026, 7, 1, 16, 58))
        self.assertFalse((self.root / "state").exists())

    def test_state_write_uses_atomic_tempfile_flush_fsync_and_replace(self):
        calls = []
        real_named_temporary_file = tempfile.NamedTemporaryFile

        class RecordingFile:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                calls.append(("write", data))
                return self.handle.write(data)

            def flush(self):
                calls.append(("flush", None))
                return self.handle.flush()

            def fileno(self):
                return self.handle.fileno()

            def __enter__(self):
                calls.append(("enter", None))
                self.handle.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                calls.append(("exit", None))
                return self.handle.__exit__(exc_type, exc, tb)

            @property
            def name(self):
                return self.handle.name

        def named_temporary_file(*args, **kwargs):
            calls.append(("tempfile", kwargs.get("dir")))
            return RecordingFile(real_named_temporary_file(*args, **kwargs))

        with mock.patch("scripts.reminder.tempfile.NamedTemporaryFile", side_effect=named_temporary_file), \
            mock.patch("scripts.reminder.os.fsync", side_effect=lambda fd: calls.append(("fsync", fd))), \
            mock.patch("scripts.reminder.os.replace", side_effect=lambda src, dst: calls.append(("replace", src, dst))):
            self.assertTrue(
                reminder.should_remind(self.root, now=datetime(2026, 7, 1, 17, 0))
            )

        self.assertIn(("tempfile", str(self.root / "state")), calls)
        self.assertTrue(any(kind == "flush" for kind, *_ in calls))
        self.assertTrue(any(kind == "fsync" for kind, *_ in calls))
        self.assertTrue(any(kind == "replace" for kind, *_ in calls))

    def test_malformed_state_creates_backup_and_raises_value_error(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "reminders.json"
        malformed = b'{"last_reminded":'
        state_path.write_bytes(malformed)

        fixed_now = datetime(2026, 7, 1, 17, 0)
        with self.assertRaisesRegex(ValueError, r"Malformed reminder state"):
            reminder.should_remind(self.root, now=fixed_now)

        self.assertEqual(state_path.read_bytes(), malformed)
        backups = list(state_dir.glob("reminders.json.broken-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertRegex(backups[0].name, r"^reminders\.json\.broken-\d{8}T\d{6}\d{6}Z\.bak$")

    def test_schema_invalid_json_list_state_creates_backup_and_raises_value_error(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "reminders.json"
        malformed = b"[]"
        state_path.write_bytes(malformed)

        with self.assertRaises(ValueError):
            reminder.should_remind(self.root, now=datetime(2026, 7, 1, 17, 0))

        self.assertEqual(state_path.read_bytes(), malformed)
        backups = list(state_dir.glob("reminders.json.broken-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertRegex(backups[0].name, r"^reminders\.json\.broken-\d{8}T\d{6}\d{6}Z\.bak$")

    def test_schema_invalid_numeric_last_reminded_creates_backup_and_raises_value_error(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "reminders.json"
        malformed = b'{"last_reminded":1}'
        state_path.write_bytes(malformed)

        with self.assertRaises(ValueError):
            reminder.should_remind(self.root, now=datetime(2026, 7, 1, 17, 0))

        self.assertEqual(state_path.read_bytes(), malformed)
        backups = list(state_dir.glob("reminders.json.broken-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertRegex(backups[0].name, r"^reminders\.json\.broken-\d{8}T\d{6}\d{6}Z\.bak$")

    def test_cli_prints_exact_stdout_for_check(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("scripts.reminder.should_remind", return_value=True), mock.patch(
            "sys.stdout", stdout
        ), mock.patch("sys.stderr", stderr):
            exit_code = reminder.main(["check", "--root", str(self.root)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "REMIND\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_returns_nonzero_and_useful_stderr_for_invalid_state(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "reminders.json").write_text("{", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        fixed_now = datetime(2026, 7, 1, 17, 0)
        with mock.patch("scripts.reminder.datetime") as fake_datetime, mock.patch(
            "sys.stdout", stdout
        ), mock.patch("sys.stderr", stderr):
            fake_datetime.now.side_effect = lambda tz=None: (
                fixed_now.replace(tzinfo=timezone.utc)
                if tz is not None
                else fixed_now
            )
            exit_code = reminder.main(["check", "--root", str(self.root)])

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Malformed reminder state", stderr.getvalue())

    def test_cross_process_race_returns_exactly_one_true_and_one_false(self):
        helper = r'''
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(r"%s")
sys.path.insert(0, str(SKILL_ROOT))
from scripts import reminder

root = Path(os.environ["REMINDER_ROOT"])
result_path = Path(os.environ["REMINDER_RESULT"])
delay = float(os.environ["REMINDER_DELAY"])
real_write_state = reminder._write_state

def slow_write_state(path, last_reminded):
    time.sleep(delay)
    return real_write_state(path, last_reminded)

reminder._write_state = slow_write_state
value = reminder.should_remind(root, now=datetime(2026, 7, 1, 17, 0))
result_path.write_text("1" if value else "0", encoding="utf-8")
''' % SKILL_ROOT

        env = os.environ.copy()
        env["REMINDER_DELAY"] = "0.75"
        p1_result = self.root / "p1.txt"
        p2_result = self.root / "p2.txt"
        p1 = subprocess.Popen(
            [sys.executable, "-c", helper],
            env={**env, "REMINDER_ROOT": str(self.root), "REMINDER_RESULT": str(p1_result)},
        )
        p2 = subprocess.Popen(
            [sys.executable, "-c", helper],
            env={**env, "REMINDER_ROOT": str(self.root), "REMINDER_RESULT": str(p2_result)},
        )
        self.assertEqual(p1.wait(timeout=10), 0)
        self.assertEqual(p2.wait(timeout=10), 0)

        outcomes = [p1_result.read_text(), p2_result.read_text()]
        self.assertEqual(outcomes.count("1"), 1)
        self.assertEqual(outcomes.count("0"), 1)

    def test_backup_name_collision_retries_with_suffix(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "reminders.json"
        malformed = b'{"last_reminded":'
        state_path.write_bytes(malformed)

        fixed_now = datetime(2026, 7, 1, 17, 0)
        base_backup = state_dir / "reminders.json.broken-20260701T170000000000Z.bak"
        base_backup.write_bytes(b"existing-backup")

        with mock.patch("scripts.reminder.datetime") as fake_datetime:
            fake_datetime.now.return_value = fixed_now.replace(tzinfo=timezone.utc)
            fake_datetime.timezone = timezone
            fake_datetime.utc = timezone.utc
            fake_datetime.strptime = datetime.strptime
            with self.assertRaises(ValueError):
                reminder.should_remind(self.root, now=fixed_now)

        next_backup = state_dir / "reminders.json.broken-20260701T170000000000Z-1.bak"
        self.assertEqual(state_path.read_bytes(), malformed)
        self.assertEqual(base_backup.read_bytes(), b"existing-backup")
        self.assertEqual(next_backup.read_bytes(), malformed)
        self.assertTrue(next_backup.exists())


if __name__ == "__main__":
    unittest.main()
