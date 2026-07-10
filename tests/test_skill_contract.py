import subprocess
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_ROOT / "SKILL.md"
REFERENCE_PATH = SKILL_ROOT / "references" / "external-ai-prompt.md"
OPENAI_PATH = SKILL_ROOT / "agents" / "openai.yaml"
TEST_REPORT_PATH = SKILL_ROOT / "tests" / "test_report.py"


EXPECTED_DESCRIPTION = (
    "Use when the user says 使用weekly-work-skill,提取5月26日工作内容; "
    "使用weekly-work-skill,汇总5月26日那一周的工作内容; "
    "使用weekly-work-skill,给prompt; legacy aliases 使用weekly-week-skill给prompt, "
    "使用weekly-work-skill给prompt, 使用weekly-week-skill,给prompt; invokes "
    "$weekly-work-skill; or asks to correct/delete weekly-work records."
)

EXPECTED_PROMPT_HEADINGS = [
    "# 周报素材",
    "## 1. 每日工作清单",
    "## 2. 阅读文献情况",
    "## 3. 研究进展情况",
    "## 4. 科研成果情况",
    "## 待确认信息",
]

EXPECTED_PROMPT_RULES = [
    "你现在是“周报素材整理助手”。请根据当前完整对话，提取本次工作中适合写入科研周报的信息。",
    "基本信息：",
    "- 日期：[填写 MM/DD，例如 06/30]",
    "- 项目：[填写项目名称]",
    "- AI 来源：[ChatGPT、Claude、Gemini 等]",
    "要求：",
    "1. 只依据对话中实际出现的内容，不推测、不夸大、不虚构。",
    "2. 删除寒暄、重复讨论、无效尝试和敏感信息。",
    "3. 保留具体任务、方法、关键结论、实验结果、产出和未解决问题。",
    "4. 日期统一使用 MM/DD 格式。",
    "5. 没有对应内容的栏目填写“无”。",
    "6. 只输出以下 Markdown，不要添加解释。",
    "# 周报素材",
    "- 日期：",
    "- 项目：",
    "- AI 来源：",
    "## 1. 每日工作清单",
    "- [已完成/进行中/待处理] 工作内容",
    "- 尽量说明使用的方法和完成结果。",
    "## 2. 阅读文献情况",
    "- 文献名称：",
    "- 作者或来源：",
    "- DOI/链接：",
    "- 阅读内容：",
    "- 关键结论：",
    "- 与当前研究的关系：",
    "## 3. 研究进展情况",
    "- 研究问题：",
    "- 使用的方法：",
    "- 完成的实验或分析：",
    "- 得到的结果：",
    "- 形成的结论：",
    "- 当前限制或待解决问题：",
    "## 4. 科研成果情况",
    "- 成果类型：论文、代码、数据集、图表、实验报告、专利或其他",
    "- 成果名称：",
    "- 完成状态：",
    "- 文件、链接或保存位置：",
    "- 可量化结果：",
    "## 待确认信息",
    "- 列出对话中缺少但可能影响周报准确性的内容。",
]

EXPECTED_DEFAULT_PROMPT = (
    "使用weekly-work-skill,提取今天工作内容 / 使用weekly-work-skill,汇总今天那一周的工作内容 / 使用weekly-work-skill,给prompt"
)

EXPECTED_OPENAI_SHORT_DESCRIPTION = (
    "跨项目记录、汇总、更正和删除每周工作，并生成结构化 Word 周报文档"
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _split_frontmatter(text: str):
    lines = text.splitlines()
    assert lines[0] == "---"
    try:
        end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise AssertionError("missing closing frontmatter fence") from exc
    frontmatter = lines[1:end]
    body = "\n".join(lines[end + 1 :])
    return frontmatter, body


def _run_help(*args: str) -> str:
    completed = subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout + completed.stderr


class SkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_has_only_required_fields(self):
        frontmatter, _ = _split_frontmatter(_read_text(SKILL_PATH))
        values = {}
        for line in frontmatter:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()

        self.assertEqual(set(values), {"name", "description"})
        self.assertEqual(values["name"], "weekly-work-skill")
        self.assertEqual(values["description"], EXPECTED_DESCRIPTION)

    def test_skill_body_uses_required_modes_triggers_and_cli_examples(self):
        text = _read_text(SKILL_PATH)
        _, body = _split_frontmatter(text)

        self.assertLessEqual(len(text.splitlines()), 500)
        self.assertNotIn("TODO", text)
        self.assertNotIn("/Users/", text)
        self.assertIn(
            "call tool codex_app__load_workspace_dependencies with empty object",
            body,
        )
        self.assertIn("read returned Python executable", body)
        self.assertIn(
            "set BUNDLED_PYTHON to that exact path for this invocation", body
        )
        self.assertIn(
            'BUNDLED_PYTHON="<Python executable returned by codex_app__load_workspace_dependencies>"',
            body,
        )
        self.assertIn(
            "Use $BUNDLED_PYTHON for report.py and quick_validate after replacing the placeholder with the tool result.",
            body,
        )

        for token in ("collect", "summarize", "correct", "delete"):
            self.assertIn(token, body)

        for token in (
            "使用weekly-work-skill,提取5月26日工作内容",
            "使用weekly-work-skill,提取5月26日至5月31日工作内容",
            "使用weekly-work-skill,汇总5月26日那一周的工作内容",
            "使用weekly-work-skill,给prompt",
            "$weekly-work-skill",
            "汇总weekly-work-skill",
            "使用 $weekly-work-skill 汇总本周周报",
        ):
            self.assertIn(token, body)

        self.assertIn("ROOT=$HOME/Documents/周报", body)
        self.assertIn(
            "SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill", body
        )
        self.assertIn("weekly_store.py add", body)
        self.assertIn("--root", body)
        self.assertIn("--today", body)
        self.assertIn("Every daily `add` result must be `added` or `duplicate` before replying exactly 收到", body)
        self.assertIn("Any `error` result -> do not claim receipt", body)
        self.assertIn("MM/DD", body)
        self.assertIn("current local year", body)
        self.assertIn("ask if missing/invalid", body)
        self.assertIn("do not generate Word during collection", body)
        self.assertIn("reply exactly 收到", body)
        self.assertIn("no headings, path, summary, or reminder", body)
        self.assertIn("If the prompt includes a date, use that date as --week-of; otherwise use local invocation date", body)
        self.assertIn("codex_app workspace dependency loader", body)
        self.assertIn("bundled Python", body)
        self.assertIn("never install/use system Python for report.py", body)
        self.assertIn("No data means no empty report", body)
        self.assertIn("return absolute Word path on success", body)
        self.assertIn("scripts/weekly_store.py", body)
        self.assertIn("scripts/report.py", body)
        self.assertIn("references/external-ai-prompt.md", body)
        self.assertIn("quick_validate.py", body)
        self.assertIn("\"$PYTHON\" \"$SKILL_DIR/scripts/weekly_store.py\" list --root \"$ROOT\" --week-of \"$TARGET_DATE\"", body)
        self.assertIn('TARGET_DATE="2026-06-30"', body)
        self.assertIn('RECORD_ID="0123456789abcdef"', body)
        self.assertIn(
            '"$PYTHON" "$SKILL_DIR/scripts/weekly_store.py" replace --root "$ROOT" --today "$TODAY" "$RECORD_ID"',
            body,
        )
        self.assertIn("\"$PYTHON\" \"$SKILL_DIR/scripts/weekly_store.py\" delete --root \"$ROOT\" --week-of \"$TARGET_DATE\" \"$RECORD_ID\"", body)
        self.assertNotIn('TARGET_DATE="$(date +%F)"', body)
        self.assertIn("list relevant week first", body)
        self.assertIn("If multiple records match, ask and stop", body)
        self.assertIn("1. 每日工作清单", body)
        self.assertIn("2. 阅读文献情况", body)
        self.assertIn("3. 研究进展情况", body)
        self.assertIn("4. 科研成果情况", body)
        self.assertIn("require exactly one record", body)
        self.assertIn("ask if ambiguous", body)
        self.assertIn("replace with complete normalized payload", body)
        self.assertIn("delete only explicit target", body)
        self.assertIn("preserve others", body)
        self.assertIn("report concise result", body)
        self.assertIn("Read references/external-ai-prompt.md only when asked for a prompt for another AI", body)
        self.assertIn("Reminder is global AGENTS responsibility", body)
        self.assertIn("while Skill active never run reminder check", body)
        self.assertIn("Include exact CLI examples without a user/version-hardcoded bundled Python", body)
        self.assertIn("No README or auxiliary docs", body)
        self.assertIn("For quick_validate, set SKILL_CREATOR from CODEX_HOME fallback and invoke $BUNDLED_PYTHON", body)
        self.assertIn("confirmed illustrative value", body)

        for snippet in (
            "date",
            "project",
            "ai_source",
            "daily_work",
            "literature",
            "research_progress",
            "research_outputs",
            "unresolved",
        ):
            self.assertIn(snippet, body)

    def test_collect_mode_uses_date_driven_project_history_discovery(self):
        _, body = _split_frontmatter(_read_text(SKILL_PATH))

        for required in (
            "single date or inclusive date range",
            "scripts/project_history.py discover",
            "state_5.sqlite",
            "project root or descendant cwd",
            "codex_app__read_thread",
            "If `codex_app__read_thread` is unavailable or not exposed",
            "scripts/project_history.py export",
            "the only rollout fallback",
            "--thread-id \"$THREAD_ID\"",
            "--start \"$START_DATE\"",
            "--end \"$END_DATE\"",
            "do not ask the user to provide the date's work content",
            "do not manually collect from user-provided summaries",
            "includeOutputs: true",
            "maxOutputCharsPerItem: 5000",
            "bounded turnLimit",
            "nextCursor",
            "hasMore is false",
            "startedAt",
            "Asia/Shanghai",
            "Never use thread `createdAt` as the work date",
            "do not read or parse rollout JSONL",
            "exclude collection-control messages",
            "group supported facts by source turn date",
            "one normalized payload per non-empty date",
            "Build all daily payloads in memory before writing",
            "No recordable project chat content",
            "do not fall back to the current chat",
            "read user messages and Codex agent messages",
            "do not copy raw logs",
            "secrets",
            '"ai_source": "Codex"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, body)

        for obsolete in (
            "extract only supplied facts",
            "本次对话里明确给出的事实",
            "ask for confirmation before persistence",
            "weekly work material means content the user explicitly wants stored",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, body)

    def test_prompt_mode_has_aliases_and_prompt_only_behavior(self):
        frontmatter, body = _split_frontmatter(_read_text(SKILL_PATH))
        description = next(
            line.split(":", 1)[1].strip()
            for line in frontmatter
            if line.startswith("description:")
        )

        aliases = (
            "使用weekly-work-skill,给prompt",
            "使用weekly-week-skill给prompt",
            "使用weekly-work-skill给prompt",
            "使用weekly-week-skill,给prompt",
        )
        for alias in aliases:
            self.assertIn(alias, description)

        self.assertIn(
            "   - 触发：`使用weekly-work-skill,给prompt`；legacy aliases: `使用weekly-week-skill给prompt`、`使用weekly-work-skill给prompt`、`使用weekly-week-skill,给prompt`。",
            body.splitlines(),
        )

        self.assertIn("五种模式互斥，只选一种", body)
        self.assertIn("5. `prompt`", body)
        self.assertIn("prompt-only and mutually exclusive", body)
        self.assertIn(
            "Read references/external-ai-prompt.md and return its complete file content exactly.",
            body,
        )
        self.assertIn("no extra commentary or code fence", body)
        self.assertIn("do not persist weekly-report data", body)
        self.assertIn("do not generate Word", body)
        self.assertIn("do not run reminder check", body)

    def test_skill_examples_match_actual_cli_flags(self):
        list_help = _run_help(
            str(SKILL_ROOT / "scripts" / "weekly_store.py"),
            "list",
            "--help",
        )
        discover_help = _run_help(
            str(SKILL_ROOT / "scripts" / "project_history.py"),
            "discover",
            "--help",
        )
        export_help = _run_help(
            str(SKILL_ROOT / "scripts" / "project_history.py"),
            "export",
            "--help",
        )
        replace_help = _run_help(
            str(SKILL_ROOT / "scripts" / "weekly_store.py"),
            "replace",
            "--help",
        )
        delete_help = _run_help(
            str(SKILL_ROOT / "scripts" / "weekly_store.py"),
            "delete",
            "--help",
        )
        report_help = _run_help(
            str(SKILL_ROOT / "scripts" / "report.py"),
            "generate",
            "--help",
        )

        for output, flags in (
            (list_help, ("--root ROOT", "--week-of WEEK_OF")),
            (
                discover_help,
                (
                    "--cwd CWD",
                    "--range DATE_RANGE",
                    "--today TODAY",
                    "--codex-home CODEX_HOME",
                    "--timezone TIMEZONE",
                ),
            ),
            (
                export_help,
                (
                    "--thread-id THREAD_ID",
                    "--start START",
                    "--end END",
                    "--codex-home CODEX_HOME",
                    "--timezone TIMEZONE",
                    "--max-chars MAX_CHARS",
                ),
            ),
            (replace_help, ("--root ROOT", "--today TODAY", "record_id")),
            (delete_help, ("--root ROOT", "--week-of WEEK_OF", "record_id")),
            (report_help, ("--root ROOT", "--week-of WEEK_OF", "--template TEMPLATE")),
        ):
            for flag in flags:
                self.assertIn(flag, output)

    def test_external_prompt_is_present_and_matches_required_headings(self):
        self.assertTrue(REFERENCE_PATH.exists(), "missing external AI prompt reference")
        text = _read_text(REFERENCE_PATH)

        for heading in EXPECTED_PROMPT_HEADINGS:
            self.assertIn(heading, text)
        for rule in EXPECTED_PROMPT_RULES:
            self.assertIn(rule, text)
        self.assertIn("Markdown", text)
        self.assertIn("待确认", text)
        self.assertIn(
            "Markdown output is weekly-work-skill input for confirmation",
            text,
        )
        self.assertIn("normalized to JSON before storage", text)
        self.assertIn("not directly used as storage schema", text)

    def test_agents_openai_default_prompt_mentions_skill(self):
        text = _read_text(OPENAI_PATH)
        self.assertIn("default_prompt", text)
        self.assertIn("weekly-work-skill", text)
        self.assertEqual(text.count("default_prompt"), 1)
        self.assertEqual(
            EXPECTED_DEFAULT_PROMPT,
            _read_text(OPENAI_PATH).split("default_prompt: ", 1)[1].split("\n", 1)[0].strip().strip('"'),
        )

    def test_agents_openai_short_description_is_complete_and_length_bounded(self):
        text = _read_text(OPENAI_PATH)
        self.assertIn("short_description", text)
        description = (
            text.split("short_description: ", 1)[1].split("\n", 1)[0].strip().strip('"')
        )
        self.assertEqual(description, EXPECTED_OPENAI_SHORT_DESCRIPTION)
        self.assertGreaterEqual(len(description), 25)
        self.assertLessEqual(len(description), 64)
        for token in ("记录", "汇总", "更正", "删除"):
            self.assertIn(token, description)

    def test_report_cli_uses_active_interpreter_without_user_specific_bundle_path(self):
        text = _read_text(TEST_REPORT_PATH)
        self.assertNotIn(".cache/codex-runtimes/", text)
        self.assertNotIn('Path("/Users/', text)
        self.assertNotIn("PYTHON = Path(", text)
        self.assertIn("sys.executable", text)
        self.assertIn("[sys.executable, str(REPORT_SCRIPT), *args]", text)

    def test_skill_uses_cross_platform_python_placeholder_and_local_script_package(self):
        body = _split_frontmatter(_read_text(SKILL_PATH))[1]
        test_source = _read_text(Path(__file__))

        self.assertIn('PYTHON="${PYTHON:-python}"', body)
        self.assertNotIn('python3 "$SKILL_DIR', body)
        self.assertNotIn('python3 "$SKILL_CREATOR', body)
        self.assertIn("[sys.executable, *args]", test_source)
        self.assertNotRegex(test_source, r"\[\s*['\"]python3['\"], \*args\]")
        self.assertTrue((SKILL_ROOT / "scripts" / "__init__.py").exists())

    def test_skill_is_directly_runnable_with_unittest_entrypoint(self):
        text = _read_text(Path(__file__))
        self.assertIn('if __name__ == "__main__":', text)
        self.assertIn("unittest.main()", text)


if __name__ == "__main__":
    unittest.main()
