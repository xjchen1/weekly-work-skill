import importlib.util
import hashlib
import json
import os
from datetime import date
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts import weekly_store


CATEGORIES = (
    "daily_work",
    "literature",
    "research_progress",
    "research_outputs",
    "unresolved",
)


class FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.calls = []

    def locking(self, fileno, mode, nbytes):
        self.calls.append((mode, nbytes))


def import_script_without_fcntl(name, relative_path, fake_msvcrt):
    script_path = SKILL_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": fake_msvcrt}):
        spec.loader.exec_module(module)
    return module


def payload(work_date="2026-06-30", project="alpha", **overrides):
    value = {
        "date": work_date,
        "project": project,
        "ai_source": "codex",
        "daily_work": ["Implemented storage"],
        "literature": [],
        "research_progress": [],
        "research_outputs": [],
        "unresolved": [],
    }
    value.update(overrides)
    return value


def expected_fingerprint(value):
    canonical = {
        "date": value["date"],
        "project": value.get("project", ""),
        "ai_source": value.get("ai_source", ""),
    }
    canonical.update({name: value.get(name, []) for name in CATEGORIES})
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DateTests(unittest.TestCase):
    def test_mm_dd_uses_today_year(self):
        self.assertEqual(
            weekly_store.normalize_date("01/02", today=date(2027, 11, 19)),
            date(2027, 1, 2),
        )

    def test_iso_date_is_accepted(self):
        self.assertEqual(
            weekly_store.normalize_date("2025-12-31"), date(2025, 12, 31)
        )

    def test_invalid_and_missing_dates_are_rejected(self):
        for value in (None, "", "2026-02-30", "6/30", "not-a-date"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                weekly_store.normalize_date(value)

    def test_week_bounds_cross_month_and_year(self):
        self.assertEqual(
            weekly_store.week_bounds(date(2025, 12, 31)),
            (date(2025, 12, 29), date(2026, 1, 4)),
        )
        self.assertEqual(
            weekly_store.week_bounds(date(2026, 3, 1)),
            (date(2026, 2, 23), date(2026, 3, 1)),
        )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_add_persists_canonical_entry_and_detects_duplicate(self):
        raw = {
            "date": "06/30",
            "project": "alpha",
            "ai_source": "codex",
            "daily_work": "Implemented storage",
        }
        first = weekly_store.add_entry(
            self.root,
            raw,
            today=date(2026, 7, 1),
            now="2026-06-30T08:00:00Z",
        )
        second = weekly_store.add_entry(
            self.root,
            raw,
            today=date(2026, 7, 2),
            now="2026-06-30T09:00:00Z",
        )

        canonical = {
            "date": "2026-06-30",
            "project": "alpha",
            "ai_source": "codex",
            "daily_work": ["Implemented storage"],
            "literature": [],
            "research_progress": [],
            "research_outputs": [],
            "unresolved": [],
        }
        fingerprint = expected_fingerprint(canonical)
        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["record_id"], fingerprint[:16])
        self.assertEqual(second["record_id"], first["record_id"])
        self.assertEqual(first["entry"]["fingerprint"], fingerprint)
        self.assertEqual(first["entry"]["collected_at"], "2026-06-30T08:00:00Z")
        self.assertEqual(second["entry"], first["entry"])
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 1)

    def test_multiple_projects_share_weekly_file_as_distinct_entries(self):
        first = weekly_store.add_entry(self.root, payload(project="alpha"))
        second = weekly_store.add_entry(self.root, payload(project="beta"))

        target = self.root / "data" / "2026-06-29.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["week_start"], "2026-06-29")
        self.assertEqual(document["week_end"], "2026-07-05")
        self.assertEqual(
            {entry["project"] for entry in document["entries"]}, {"alpha", "beta"}
        )

    def test_storage_lock_falls_back_to_windows_msvcrt_when_fcntl_is_unavailable(self):
        fake_msvcrt = FakeMsvcrt()
        module = import_script_without_fcntl(
            "weekly_store_no_fcntl",
            Path("scripts") / "weekly_store.py",
            fake_msvcrt,
        )

        with tempfile.TemporaryDirectory() as temporary:
            with module._storage_lock(Path(temporary)):
                pass

        self.assertEqual(
            fake_msvcrt.calls,
            [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)],
        )

    def test_concurrent_adds_do_not_lose_entries(self):
        first_read = self.root / "first-read"
        second_read = self.root / "second-read"
        release = self.root / "release-first"
        helper = r'''
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
from scripts import weekly_store

root = Path(sys.argv[2])
project = sys.argv[3]
marker = Path(sys.argv[4])
release = Path(sys.argv[5])
real_read = weekly_store._read_document

def observed_read(path, day):
    document = real_read(path, day)
    marker.write_text("ready", encoding="utf-8")
    if project == "alpha":
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for release")
            time.sleep(0.01)
    return document

weekly_store._read_document = observed_read
result = weekly_store.add_entry(
    root,
    {
        "date": "2026-06-30",
        "project": project,
        "ai_source": "codex",
        "daily_work": ["work from " + project],
        "literature": [],
        "research_progress": [],
        "research_outputs": [],
        "unresolved": [],
    },
)
print(json.dumps(result, ensure_ascii=False))
'''

        first = subprocess.Popen(
            [
                sys.executable,
                "-c",
                helper,
                str(SKILL_ROOT),
                str(self.root),
                "alpha",
                str(first_read),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not first_read.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(first_read.exists(), "first process never reached the read")

        second = subprocess.Popen(
            [
                sys.executable,
                "-c",
                helper,
                str(SKILL_ROOT),
                str(self.root),
                "beta",
                str(second_read),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 1
        while not second_read.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        release.write_text("go", encoding="utf-8")

        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second_stderr)
        self.assertTrue(first_stdout.strip())
        self.assertTrue(second_stdout.strip())
        entries = weekly_store.list_entries(self.root, date(2026, 6, 30))
        self.assertEqual({entry["project"] for entry in entries}, {"alpha", "beta"})

    def test_omitted_project_and_ai_source_are_stored_as_empty_strings(self):
        added = weekly_store.add_entry(
            self.root,
            {
                "date": "2026-06-30",
                "daily_work": ["Worked without optional metadata"],
            },
        )

        target = self.root / "data" / "2026-06-29.json"
        persisted = json.loads(target.read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(added["entry"]["project"], "")
        self.assertEqual(added["entry"]["ai_source"], "")
        self.assertEqual(persisted["project"], "")
        self.assertEqual(persisted["ai_source"], "")

    def test_distinct_content_in_same_project_has_distinct_ids_and_entries(self):
        first = weekly_store.add_entry(
            self.root, payload(project="alpha", daily_work=["First task"])
        )
        second = weekly_store.add_entry(
            self.root, payload(project="alpha", daily_work=["Second task"])
        )

        entries = weekly_store.list_entries(self.root, date(2026, 6, 30))
        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "added")
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertNotEqual(
            first["entry"]["fingerprint"], second["entry"]["fingerprint"]
        )
        self.assertEqual(len(entries), 2)

    def test_terminal_sentence_punctuation_is_ignored_for_category_deduplication(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录能够持久化。"],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录能够持久化!!!"],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 1)

    def test_whitespace_normalization_is_used_for_deduplication(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                project="  alpha   beta  ",
                ai_source="  codex \t cli  ",
                research_progress=["  确认   测试记录\t能够持久化  "],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                project="alpha beta",
                ai_source="codex cli",
                research_progress=["确认 测试记录 能够持久化"],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 1)

    def test_recursive_normalization_walks_category_lists_and_dict_values(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[
                    {
                        "summary": "  形成   初稿。 ",
                        "notes": ["  待复核\t", {"detail": "确认   结果；；"}],
                    }
                ],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[
                    {
                        "summary": "形成 初稿",
                        "notes": ["待复核", {"detail": "确认 结果"}],
                    }
                ],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 1)

    def test_duplicate_keeps_first_entry_original_content(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录能够持久化。"],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录能够持久化"],
                daily_work=[],
            ),
        )

        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["entry"]["research_progress"], ["确认测试记录能够持久化。"])
        self.assertEqual(second["entry"]["research_progress"], ["确认测试记录能够持久化。"])
        persisted = weekly_store.list_entries(self.root, date(2026, 6, 30))
        self.assertEqual(persisted[0]["research_progress"], ["确认测试记录能够持久化。"])

    def test_meaningful_internal_punctuation_difference_remains_distinct(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录，能够持久化"],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_progress=["确认测试记录能够持久化"],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "added")
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 2)

    def test_identifier_like_nested_fields_preserve_terminal_punctuation(self):
        identifier_keys = (
            ("DOI", "10.1000/example."),
            ("doi", "10.1000/example;"),
            ("URL", "https://example.com/path?"),
            ("url", "https://example.com/file."),
            ("link", "internal-ref;"),
            ("链接", "文档入口？"),
            ("path", "/tmp/output;"),
            ("路径", "/tmp/output."),
            ("file", "report.md."),
            ("文件", "记录；"),
            ("location", "s3://bucket/key?"),
            ("citation_key", "smith2024."),
            ("id", "abc123;"),
        )

        for field, punctuated in identifier_keys:
            with self.subTest(field=field, value=punctuated):
                first = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: punctuated}],
                        daily_work=[],
                    ),
                )
                second = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: punctuated[:-1]}],
                        daily_work=[],
                    ),
                )

                self.assertEqual(first["status"], "added")
                self.assertEqual(second["status"], "added")
                self.assertNotEqual(
                    first["entry"]["fingerprint"], second["entry"]["fingerprint"]
                )

    def test_punctuation_only_category_string_does_not_normalize_to_empty(self):
        first = weekly_store.add_entry(
            self.root,
            payload(research_progress=["!!!"], daily_work=[]),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(research_progress=[""], daily_work=[]),
        )
        third = weekly_store.add_entry(
            self.root,
            payload(research_progress=["；"], daily_work=[]),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "added")
        self.assertEqual(third["status"], "added")
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 3)

    def test_dict_keys_remain_schema_significant_for_fingerprint(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"summary": "形成初稿"}],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"summary ": "形成初稿"}],
                daily_work=[],
            ),
        )
        third = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"summary;": "形成初稿"}],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "added")
        self.assertEqual(third["status"], "added")
        self.assertEqual(len(weekly_store.list_entries(self.root, date(2026, 6, 30))), 3)

    def test_nfc_equivalent_prose_dedupes_for_fingerprint(self):
        composed = "Caf\u00e9"
        decomposed = unicodedata.normalize("NFD", composed)

        first = weekly_store.add_entry(
            self.root,
            payload(research_progress=[composed], daily_work=[]),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(research_progress=[decomposed], daily_work=[]),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])

    def test_combined_chinese_identifier_keys_preserve_terminal_punctuation(self):
        for field in ("DOI/链接", "文件、链接或保存位置"):
            with self.subTest(field=field):
                first = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: "参考资料."}],
                        daily_work=[],
                    ),
                )
                second = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: "参考资料"}],
                        daily_work=[],
                    ),
                )

                self.assertEqual(first["status"], "added")
                self.assertEqual(second["status"], "added")
                self.assertNotEqual(
                    first["entry"]["fingerprint"], second["entry"]["fingerprint"]
                )

    def test_identifier_key_variants_preserve_terminal_punctuation(self):
        for field, punctuated in (
            ("file_path", "/tmp/report;"),
            ("citationKey", "smith2024?"),
            ("filePath", "/tmp/report."),
            (" URL ", "https://example.com/path?"),
        ):
            with self.subTest(field=field):
                first = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: punctuated}],
                        daily_work=[],
                    ),
                )
                second = weekly_store.add_entry(
                    self.root,
                    payload(
                        research_outputs=[{field: punctuated[:-1]}],
                        daily_work=[],
                    ),
                )

                self.assertEqual(first["status"], "added")
                self.assertEqual(second["status"], "added")
                self.assertNotEqual(
                    first["entry"]["fingerprint"], second["entry"]["fingerprint"]
                )

    def test_profile_key_remains_prose_like_for_terminal_punctuation(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"profile": "形成画像。"}],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"profile": "形成画像"}],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])

    def test_identifier_values_still_dedupe_after_whitespace_normalization(self):
        first = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"filePath": "  /tmp/report.md  "}],
                daily_work=[],
            ),
        )
        second = weekly_store.add_entry(
            self.root,
            payload(
                research_outputs=[{"filePath": "/tmp/report.md"}],
                daily_work=[],
            ),
        )

        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entry"], first["entry"])

    def test_list_uses_the_week_containing_requested_date(self):
        added = weekly_store.add_entry(self.root, payload())

        for day in (date(2026, 6, 29), date(2026, 7, 1), date(2026, 7, 5)):
            with self.subTest(day=day):
                self.assertEqual(
                    weekly_store.list_entries(self.root, day), [added["entry"]]
                )
        self.assertEqual(weekly_store.list_entries(self.root, date(2026, 7, 6)), [])

    def test_replace_updates_fingerprint_and_preserves_other_entries(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        other = weekly_store.add_entry(self.root, payload(project="beta"))

        replacement_payload = payload(
            project="alpha", daily_work=["Implemented and tested storage"]
        )
        replaced = weekly_store.replace_entry(
            self.root,
            original["record_id"],
            replacement_payload,
            today=date(2026, 6, 30),
        )

        entries = weekly_store.list_entries(self.root, date(2026, 7, 5))
        self.assertEqual(replaced["status"], "replaced")
        self.assertNotEqual(replaced["record_id"], original["record_id"])
        self.assertEqual(replaced["entry"]["fingerprint"], expected_fingerprint(replacement_payload))
        self.assertEqual(
            {entry["record_id"] for entry in entries},
            {replaced["record_id"], other["record_id"]},
        )

    def test_replace_ignores_unrelated_json_filenames(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        notes_path = self.root / "data" / "notes.json"
        notes_content = b'{"note":"keep this file"}\n'
        notes_path.write_bytes(notes_content)

        try:
            replaced = weekly_store.replace_entry(
                self.root,
                original["record_id"],
                payload(project="alpha", daily_work=["Replacement content"]),
            )
        except ValueError as exc:
            self.fail("unrelated JSON blocked replace: %s" % exc)

        self.assertEqual(replaced["status"], "replaced")
        self.assertEqual(notes_path.read_bytes(), notes_content)

    def test_replace_surfaces_malformed_valid_weekly_file_alongside_healthy_file(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        malformed_path = self.root / "data" / "2026-07-06.json"
        malformed = b'{"schema_version":1,"entries":['
        malformed_path.write_bytes(malformed)

        with self.assertRaisesRegex(
            ValueError, r"Malformed weekly JSON in .*2026-07-06\.json"
        ):
            weekly_store.replace_entry(
                self.root,
                original["record_id"],
                payload(project="alpha", daily_work=["Replacement content"]),
            )

        backups = list(
            malformed_path.parent.glob(malformed_path.name + ".broken-*.bak")
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertEqual(malformed_path.read_bytes(), malformed)

    def test_replace_can_move_an_entry_without_losing_other_records(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        other = weekly_store.add_entry(self.root, payload(project="beta"))

        moved = weekly_store.replace_entry(
            self.root,
            original["record_id"],
            payload(work_date="2026-07-07", project="alpha"),
        )

        self.assertEqual(
            weekly_store.list_entries(self.root, date(2026, 6, 30)), [other["entry"]]
        )
        self.assertEqual(
            weekly_store.list_entries(self.root, date(2026, 7, 7)), [moved["entry"]]
        )

    def test_cross_week_replace_rolls_back_both_files_when_second_write_fails(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        weekly_store.add_entry(
            self.root, payload(work_date="2026-07-07", project="destination-existing")
        )
        source_path = self.root / "data" / "2026-06-29.json"
        destination_path = self.root / "data" / "2026-07-06.json"
        source_before = source_path.read_bytes()
        destination_before = destination_path.read_bytes()
        original_write = weekly_store._write_document
        write_count = 0

        def fail_second_write(path, document):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise OSError("injected second-write failure")
            return original_write(path, document)

        with mock.patch.object(
            weekly_store, "_write_document", side_effect=fail_second_write
        ):
            with self.assertRaisesRegex(OSError, "injected second-write failure"):
                weekly_store.replace_entry(
                    self.root,
                    original["record_id"],
                    payload(work_date="2026-07-07", project="alpha"),
                )

        self.assertEqual(source_path.read_bytes(), source_before)
        self.assertEqual(destination_path.read_bytes(), destination_before)

    def test_cross_week_rollback_attempts_destination_after_source_restore_fails(self):
        original = weekly_store.add_entry(self.root, payload(project="alpha"))
        weekly_store.add_entry(
            self.root, payload(work_date="2026-07-07", project="destination-existing")
        )
        source_path = self.root / "data" / "2026-06-29.json"
        destination_path = self.root / "data" / "2026-07-06.json"
        destination_before = destination_path.read_bytes()
        original_write = weekly_store._write_document
        original_atomic_write = weekly_store._write_bytes_atomic
        write_count = 0
        rollback_paths = []

        def fail_second_write(path, document):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise OSError("original operation failure")
            return original_write(path, document)

        def fail_source_rollback(path, content):
            if write_count == 1:
                return original_atomic_write(path, content)
            rollback_paths.append(path)
            if path == source_path:
                raise OSError("source rollback failure")
            return original_atomic_write(path, content)

        with mock.patch.object(
            weekly_store, "_write_document", side_effect=fail_second_write
        ), mock.patch.object(
            weekly_store, "_write_bytes_atomic", side_effect=fail_source_rollback
        ):
            try:
                weekly_store.replace_entry(
                    self.root,
                    original["record_id"],
                    payload(work_date="2026-07-07", project="alpha"),
                )
            except Exception as exc:
                caught = exc
            else:
                self.fail("cross-week replace unexpectedly succeeded")

        self.assertEqual(rollback_paths, [source_path, destination_path])
        self.assertEqual(destination_path.read_bytes(), destination_before)
        self.assertIsInstance(caught, RuntimeError)
        self.assertIn("original operation failure", str(caught))
        self.assertIn("source rollback failure", str(caught))
        self.assertIsInstance(caught.__cause__, OSError)
        self.assertEqual(str(caught.__cause__), "original operation failure")

    def test_delete_removes_only_target(self):
        first = weekly_store.add_entry(self.root, payload(project="alpha"))
        second = weekly_store.add_entry(self.root, payload(project="beta"))

        self.assertTrue(
            weekly_store.delete_entry(
                self.root, first["record_id"], week_of=date(2026, 7, 2)
            )
        )
        self.assertFalse(
            weekly_store.delete_entry(
                self.root, first["record_id"], week_of=date(2026, 7, 2)
            )
        )
        self.assertEqual(
            weekly_store.list_entries(self.root, date(2026, 7, 2)), [second["entry"]]
        )

    def test_has_entries_for_date_matches_exact_work_date(self):
        weekly_store.add_entry(self.root, payload(work_date="2026-06-30"))

        self.assertTrue(weekly_store.has_entries_for_date(self.root, date(2026, 6, 30)))
        self.assertFalse(weekly_store.has_entries_for_date(self.root, date(2026, 7, 1)))
        self.assertFalse(weekly_store.has_entries_for_date(self.root, date(2026, 7, 6)))

    def test_malformed_json_is_backed_up_and_never_overwritten(self):
        target = self.root / "data" / "2026-06-29.json"
        target.parent.mkdir(parents=True)
        malformed = b'{"schema_version": 1, broken'
        target.write_bytes(malformed)

        with self.assertRaisesRegex(ValueError, "Malformed weekly JSON"):
            weekly_store.add_entry(self.root, payload())

        backups = list(target.parent.glob(target.name + ".broken-*.bak"))
        self.assertEqual(target.read_bytes(), malformed)
        self.assertEqual(len(backups), 1)
        self.assertRegex(
            backups[0].name,
            r"^2026-06-29\.json\.broken-\d{8}T\d{12}Z\.bak$",
        )
        self.assertEqual(backups[0].read_bytes(), malformed)

    def test_structurally_invalid_weekly_json_is_backed_up(self):
        target = self.root / "data" / "2026-06-29.json"
        target.parent.mkdir(parents=True)
        malformed = b'{"schema_version":99,"entries":[]}'
        target.write_bytes(malformed)

        with self.assertRaisesRegex(ValueError, "Unsupported schema version"):
            weekly_store.add_entry(self.root, payload())

        backups = list(target.parent.glob(target.name + ".broken-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertEqual(target.read_bytes(), malformed)

    def test_writes_use_complete_same_directory_temp_file_and_replace(self):
        target = self.root / "data" / "2026-06-29.json"
        original_replace = os.replace
        observation = {}

        def inspect_then_replace(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            observation["same_directory"] = source_path.parent == destination_path.parent
            observation["source_document"] = json.loads(
                source_path.read_text(encoding="utf-8")
            )
            observation["target_existed"] = destination_path.exists()
            original_replace(source, destination)

        with mock.patch.object(
            weekly_store.os, "replace", side_effect=inspect_then_replace
        ) as replace:
            weekly_store.add_entry(self.root, payload())

        self.assertEqual(replace.call_count, 1)
        self.assertTrue(observation["same_directory"])
        self.assertFalse(observation["target_existed"])
        self.assertEqual(len(observation["source_document"]["entries"]), 1)
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_atomic_write_flushes_fsyncs_and_replaces_in_order(self):
        original_named_temporary_file = tempfile.NamedTemporaryFile
        original_fsync = os.fsync
        original_replace = os.replace
        calls = []

        class ObservedTemporaryFile:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.name = wrapped.name

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *arguments):
                return self.wrapped.__exit__(*arguments)

            def write(self, content):
                return self.wrapped.write(content)

            def flush(self):
                calls.append("flush")
                return self.wrapped.flush()

            def fileno(self):
                return self.wrapped.fileno()

        def observed_named_temporary_file(*arguments, **keywords):
            return ObservedTemporaryFile(
                original_named_temporary_file(*arguments, **keywords)
            )

        def observed_fsync(file_descriptor):
            calls.append("fsync")
            return original_fsync(file_descriptor)

        def observed_replace(source, destination):
            calls.append("replace")
            return original_replace(source, destination)

        with mock.patch.object(
            weekly_store.tempfile,
            "NamedTemporaryFile",
            side_effect=observed_named_temporary_file,
        ), mock.patch.object(
            weekly_store.os, "fsync", side_effect=observed_fsync
        ), mock.patch.object(
            weekly_store.os, "replace", side_effect=observed_replace
        ):
            weekly_store.add_entry(self.root, payload())

        self.assertEqual(calls, ["flush", "fsync", "replace"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.script = SKILL_ROOT / "scripts" / "weekly_store.py"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *arguments, stdin=None):
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_supports_add_list_replace_has_date_and_delete(self):
        add = self.run_cli(
            "add",
            "--root",
            str(self.root),
            "--today",
            "2026-06-30",
            stdin=json.dumps(payload(work_date="06/30")),
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        added = json.loads(add.stdout)

        listed = self.run_cli(
            "list", "--root", str(self.root), "--week-of", "2026-07-05"
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), [added["entry"]])

        has_date = self.run_cli(
            "has-date", "--root", str(self.root), "--date", "2026-06-30"
        )
        self.assertEqual(has_date.returncode, 0, has_date.stderr)
        self.assertEqual(json.loads(has_date.stdout), {"has_entries": True})

        replacement_payload = payload(daily_work=["Changed through CLI"])
        replace = self.run_cli(
            "replace",
            "--root",
            str(self.root),
            added["record_id"],
            stdin=json.dumps(replacement_payload),
        )
        self.assertEqual(replace.returncode, 0, replace.stderr)
        replaced = json.loads(replace.stdout)

        delete = self.run_cli(
            "delete",
            "--root",
            str(self.root),
            replaced["record_id"],
            "--week-of",
            "2026-06-30",
        )
        self.assertEqual(delete.returncode, 0, delete.stderr)
        self.assertEqual(json.loads(delete.stdout), {"deleted": True})

    def test_cli_rejects_non_object_or_multiple_stdin_values(self):
        for stdin in ("[]", "{} {}", "not-json"):
            with self.subTest(stdin=stdin):
                result = self.run_cli(
                    "add", "--root", str(self.root), stdin=stdin
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(result.stdout, "")

    def test_cli_stdout_is_compact_utf8_json_with_one_trailing_newline(self):
        chinese_payload = payload(
            project="中文项目",
            daily_work=["完成每周存储"],
            unresolved=["等待评审"],
        )

        result = self.run_cli(
            "add",
            "--root",
            str(self.root),
            stdin=json.dumps(chinese_payload, ensure_ascii=False),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        expected = json.dumps(
            parsed, ensure_ascii=False, separators=(",", ":")
        ) + "\n"
        self.assertEqual(result.stdout, expected)
        self.assertIn("中文项目", result.stdout)
        self.assertIn("完成每周存储", result.stdout)
        self.assertNotIn("\\u", result.stdout)
        self.assertEqual(result.stdout.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
