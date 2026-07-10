import json
import sqlite3
import sys
import subprocess
import unittest
from unittest import mock
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts import project_history


SCRIPT_PATH = SKILL_ROOT / "scripts" / "project_history.py"


def epoch(year, month, day, hour=0, minute=0, second=0):
    return int(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).timestamp()
    )


def create_thread_database(path, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                cwd TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                agent_role TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO threads
                (id, created_at, updated_at, cwd, archived, agent_role)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


class DateRangeTests(unittest.TestCase):
    def test_single_dates_use_current_year_when_omitted(self):
        today = date(2026, 7, 6)
        for value in ("7月1号", "07/01"):
            with self.subTest(value=value):
                self.assertEqual(
                    project_history.parse_date_range(value, today),
                    (date(2026, 7, 1), date(2026, 7, 1)),
                )
        self.assertEqual(
            project_history.parse_date_range("2026-07-01", today),
            (date(2026, 7, 1), date(2026, 7, 1)),
        )

    def test_inclusive_ranges_support_chinese_slash_and_iso_forms(self):
        today = date(2026, 7, 6)
        expected = (date(2026, 7, 1), date(2026, 7, 5))
        for value in (
            "7月1号到5号",
            "7月1日到7月5日",
            "07/01-07/05",
            "2026-07-01至2026-07-05",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    project_history.parse_date_range(value, today), expected
                )

    def test_implicit_cross_year_and_reversed_ranges_are_rejected(self):
        today = date(2026, 12, 31)
        self.assertEqual(
            project_history.parse_date_range(
                "2026-12-30至2027-01-02", today
            ),
            (date(2026, 12, 30), date(2027, 1, 2)),
        )
        with self.assertRaisesRegex(ValueError, "explicit years"):
            project_history.parse_date_range("12月30号到1月2号", today)
        with self.assertRaisesRegex(ValueError, "start date is after end date"):
            project_history.parse_date_range(
                "2026-07-05至2026-07-01", date(2026, 7, 6)
            )


class ProjectRootTests(unittest.TestCase):
    def test_git_root_is_preferred_and_plain_directory_is_fallback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            nested = repository / "nested"
            nested.mkdir(parents=True)
            subprocess.run(
                ["git", "init", str(repository)], check=True, capture_output=True
            )
            self.assertEqual(
                project_history.resolve_project_root(nested), repository.resolve()
            )

            plain = root / "plain"
            plain.mkdir()
            self.assertEqual(
                project_history.resolve_project_root(plain), plain.resolve()
            )

    def test_missing_path_and_regular_file_are_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                project_history.resolve_project_root(root / "missing")

            regular_file = root / "file.txt"
            regular_file.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(NotADirectoryError):
                project_history.resolve_project_root(regular_file)

    def test_missing_git_binary_falls_back_to_resolved_directory(self):
        with TemporaryDirectory() as temporary:
            plain = Path(temporary) / "plain"
            plain.mkdir()
            with mock.patch.object(
                project_history.subprocess,
                "run",
                side_effect=FileNotFoundError("git"),
            ):
                self.assertEqual(
                    project_history.resolve_project_root(plain), plain.resolve()
                )


class DatabaseLocationTests(unittest.TestCase):
    def test_root_database_is_preferred_then_nested_database_is_used(self):
        with TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            nested = codex_home / "sqlite" / "state_5.sqlite"
            create_thread_database(nested)
            self.assertEqual(project_history.find_state_database(codex_home), nested)

            root = codex_home / "state_5.sqlite"
            create_thread_database(root)
            self.assertEqual(project_history.find_state_database(codex_home), root)

    def test_missing_default_databases_raise_useful_error(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FileNotFoundError, "state_5.sqlite"):
                project_history.find_state_database(Path(temporary))


class ThreadDiscoveryTests(unittest.TestCase):
    def test_reads_uncheckpointed_wal_without_changing_database_or_wal(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = (root / "project").resolve()
            project.mkdir()
            database = root / "state_5.sqlite"
            wal_path = database.with_name(database.name + "-wal")
            writer = sqlite3.connect(str(database))
            try:
                journal_mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                self.assertEqual(journal_mode, "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        cwd TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        agent_role TEXT
                    )
                    """
                )
                writer.execute(
                    """
                    INSERT INTO threads
                        (id, created_at, updated_at, cwd, archived, agent_role)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "wal-thread",
                        epoch(2026, 7, 1),
                        epoch(2026, 7, 2),
                        str(project),
                        0,
                        None,
                    ),
                )
                writer.commit()
                self.assertTrue(wal_path.is_file())
                self.assertGreater(wal_path.stat().st_size, 0)
                before = {
                    database.name: database.read_bytes(),
                    wal_path.name: wal_path.read_bytes(),
                }

                threads = project_history.discover_project_threads(
                    database,
                    project,
                    date(2026, 7, 1),
                    date(2026, 7, 5),
                )

                self.assertEqual(
                    threads,
                    [
                        {
                            "id": "wal-thread",
                            "created_at": epoch(2026, 7, 1),
                            "updated_at": epoch(2026, 7, 2),
                            "archived": False,
                        }
                    ],
                )
                after = {
                    database.name: database.read_bytes(),
                    wal_path.name: wal_path.read_bytes(),
                }
                self.assertEqual(after, before)
            finally:
                writer.close()

    def test_maximum_end_date_raises_clear_value_error(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            database = root / "state_5.sqlite"
            create_thread_database(database)

            with self.assertRaisesRegex(ValueError, "end date has no following day"):
                project_history.discover_project_threads(
                    database, project, date.max, date.max
                )

    def test_connection_uri_is_exactly_read_only(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = (root / "project").resolve()
            project.mkdir()
            database = (root / "state_5.sqlite").resolve()
            create_thread_database(database)

            with mock.patch.object(
                project_history.sqlite3, "connect", wraps=sqlite3.connect
            ) as connect:
                project_history.discover_project_threads(
                    database,
                    project,
                    date(2026, 7, 1),
                    date(2026, 7, 5),
                )

            self.assertEqual(
                connect.call_args.args,
                (database.as_uri() + "?mode=ro",),
            )
            self.assertEqual(connect.call_args.kwargs, {"uri": True})

    def test_exact_project_overlap_role_archived_and_ordering(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = (root / "project").resolve()
            subdirectory = project / "subdir"
            sibling = (root / "project-sibling").resolve()
            subdirectory.mkdir(parents=True)
            sibling.mkdir()
            database = root / "state_5.sqlite"
            start_epoch = epoch(2026, 7, 1)
            end_epoch = epoch(2026, 7, 6)
            rows = [
                (
                    "before-created-updated-in-range",
                    epoch(2026, 6, 20),
                    start_epoch,
                    str(project),
                    0,
                    None,
                ),
                (
                    "subdirectory-thread",
                    epoch(2026, 7, 1),
                    epoch(2026, 7, 2),
                    str(subdirectory),
                    0,
                    None,
                ),
                (
                    "same-time-b",
                    epoch(2026, 7, 2),
                    epoch(2026, 7, 4),
                    str(project),
                    0,
                    "",
                ),
                (
                    "same-time-a",
                    epoch(2026, 7, 2),
                    epoch(2026, 7, 3),
                    str(project),
                    1,
                    None,
                ),
                (
                    "sibling-prefix",
                    epoch(2026, 7, 2),
                    epoch(2026, 7, 3),
                    str(sibling),
                    0,
                    None,
                ),
                (
                    "agent-thread",
                    epoch(2026, 7, 2),
                    epoch(2026, 7, 3),
                    str(project),
                    0,
                    "reviewer",
                ),
                (
                    "ended-before-range",
                    epoch(2026, 6, 1),
                    start_epoch - 1,
                    str(project),
                    0,
                    None,
                ),
                (
                    "created-at-exclusive-end",
                    end_epoch,
                    end_epoch,
                    str(project),
                    0,
                    None,
                ),
            ]
            create_thread_database(database, rows)
            wal_path = database.with_name(database.name + "-wal")
            tracked_paths = (database, wal_path)
            before = {
                path.name: path.read_bytes()
                for path in tracked_paths
                if path.is_file()
            }

            threads = project_history.discover_project_threads(
                database,
                project,
                date(2026, 7, 1),
                date(2026, 7, 5),
            )

            self.assertEqual(
                threads,
                [
                    {
                        "id": "before-created-updated-in-range",
                        "created_at": epoch(2026, 6, 20),
                        "updated_at": start_epoch,
                        "archived": False,
                    },
                    {
                        "id": "subdirectory-thread",
                        "created_at": epoch(2026, 7, 1),
                        "updated_at": epoch(2026, 7, 2),
                        "archived": False,
                    },
                    {
                        "id": "same-time-a",
                        "created_at": epoch(2026, 7, 2),
                        "updated_at": epoch(2026, 7, 3),
                        "archived": True,
                    },
                    {
                        "id": "same-time-b",
                        "created_at": epoch(2026, 7, 2),
                        "updated_at": epoch(2026, 7, 4),
                        "archived": False,
                    },
                ],
            )
            after = {
                path.name: path.read_bytes()
                for path in tracked_paths
                if path.is_file()
            }
            self.assertEqual(after, before)


class LocalThreadExportTests(unittest.TestCase):
    def test_exports_thread_messages_from_rollout_path_by_local_date(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            database = codex_home / "state_5.sqlite"
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-07-02T16:30:00Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "previous day in UTC but July 3 local",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-03T02:00:00Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "implemented feature",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-03T03:00:00Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "output": "tool result\nwith useful evidence",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-04T16:00:00Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "outside"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            create_thread_database(database)
            with sqlite3.connect(str(database)) as connection:
                connection.execute(
                    """
                    INSERT INTO threads
                        (id, rollout_path, created_at, updated_at, cwd, archived, agent_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "thread-1",
                        str(rollout),
                        epoch(2026, 7, 3),
                        epoch(2026, 7, 4),
                        str(root),
                        0,
                        None,
                    ),
                )

            exported = project_history.export_thread_messages(
                database,
                "thread-1",
                date(2026, 7, 3),
                date(2026, 7, 3),
                "Asia/Shanghai",
                max_chars=12,
            )

            self.assertEqual(exported["thread_id"], "thread-1")
            self.assertEqual(
                [item["role"] for item in exported["items"]],
                ["user", "assistant", "tool"],
            )
            self.assertEqual(
                [item["local_date"] for item in exported["items"]],
                ["2026-07-03", "2026-07-03", "2026-07-03"],
            )
            self.assertEqual(
                [item["text"] for item in exported["items"]],
                ["previous day", "implemented ", "tool result\n"],
            )

    def test_export_rejects_missing_rollout_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state_5.sqlite"
            create_thread_database(database)
            with sqlite3.connect(str(database)) as connection:
                connection.execute(
                    """
                    INSERT INTO threads
                        (id, created_at, updated_at, cwd, archived, agent_role)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "thread-1",
                        epoch(2026, 7, 3),
                        epoch(2026, 7, 4),
                        str(root),
                        0,
                        None,
                    ),
                )

            with self.assertRaisesRegex(FileNotFoundError, "rollout path"):
                project_history.export_thread_messages(
                    database,
                    "thread-1",
                    date(2026, 7, 3),
                    date(2026, 7, 3),
                )


class CommandLineTests(unittest.TestCase):
    def test_maximum_end_date_error_uses_stderr_and_exit_two(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            project = root / "project"
            project.mkdir()
            create_thread_database(codex_home / "state_5.sqlite")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "discover",
                    "--cwd",
                    str(project),
                    "--range",
                    "9999-12-31",
                    "--today",
                    "9999-12-31",
                    "--codex-home",
                    str(codex_home),
                    "--timezone",
                    "Asia/Shanghai",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertRegex(
                completed.stderr,
                r"^error: end date has no following day: 9999-12-31\n$",
            )
            self.assertNotIn("Traceback", completed.stderr)

    def test_discover_outputs_compact_utf8_json(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            project = (root / "项目").resolve()
            project.mkdir()
            database = codex_home / "state_5.sqlite"
            create_thread_database(
                database,
                [
                    (
                        "thread-1",
                        epoch(2026, 7, 1),
                        epoch(2026, 7, 2),
                        str(project),
                        0,
                        None,
                    )
                ],
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "discover",
                    "--cwd",
                    str(project),
                    "--range",
                    "7月1号到5号",
                    "--today",
                    "2026-07-06",
                    "--codex-home",
                    str(codex_home),
                    "--timezone",
                    "Asia/Shanghai",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertNotIn(": ", completed.stdout)
            self.assertIn("项目", completed.stdout)
            self.assertNotIn("\\u9879", completed.stdout)
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "project_root": str(project),
                    "start": "2026-07-01",
                    "end": "2026-07-05",
                    "threads": [
                        {
                            "id": "thread-1",
                            "created_at": epoch(2026, 7, 1),
                            "updated_at": epoch(2026, 7, 2),
                            "archived": False,
                        }
                    ],
                },
            )

    def test_export_outputs_local_thread_messages(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            database = codex_home / "state_5.sqlite"
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-03T01:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "work item"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            create_thread_database(database)
            with sqlite3.connect(str(database)) as connection:
                connection.execute(
                    """
                    INSERT INTO threads
                        (id, rollout_path, created_at, updated_at, cwd, archived, agent_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "thread-1",
                        str(rollout),
                        epoch(2026, 7, 3),
                        epoch(2026, 7, 4),
                        str(root),
                        0,
                        None,
                    ),
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "export",
                    "--thread-id",
                    "thread-1",
                    "--start",
                    "2026-07-03",
                    "--end",
                    "2026-07-03",
                    "--codex-home",
                    str(codex_home),
                    "--timezone",
                    "Asia/Shanghai",
                    "--max-chars",
                    "5000",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "thread_id": "thread-1",
                    "items": [
                        {
                            "timestamp": "2026-07-03T01:00:00Z",
                            "local_date": "2026-07-03",
                            "role": "user",
                            "type": "message",
                            "text": "work item",
                        }
                    ],
                },
            )

    def test_discover_errors_use_stderr_and_exit_two(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "discover",
                    "--cwd",
                    str(project),
                    "--range",
                    "not-a-date",
                    "--today",
                    "2026-07-06",
                    "--codex-home",
                    str(root / "missing-codex-home"),
                    "--timezone",
                    "Asia/Shanghai",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertRegex(completed.stderr, r"^error: .+\n$")


if __name__ == "__main__":
    unittest.main()
