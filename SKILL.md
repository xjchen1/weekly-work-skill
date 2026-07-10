---
name: weekly-work-skill
description: Use when the user says 使用weekly-work-skill, 使用weekly-week-skill, 使用weekly-week-skill给prompt, or 使用weekly-work-skill给prompt, invokes $weekly-work-skill, asks to record weekly work, says 汇总weekly-work-skill, requests a current-week Word report, or asks to correct or delete stored weekly-work records.
---

# Weekly Work Skill

## Overview

这个 skill 负责把每周工作素材整理成可存储、可汇总、可修正、可删除的结构化记录，并在需要时生成当前周的 Word 周报。

## Mode Selection

五种模式互斥，只选一种：

1. `collect`
- 触发：`使用weekly-work-skill`、显式 `$weekly-work-skill`，或用户明确要求按日期记录 weekly-report material。
- 目标：用户只提供日期；从当前项目的 Codex 历史对话中提取对应日期的工作事实并存储。

2. `summarize`
   - 触发：`汇总weekly-work-skill` 或 `使用 $weekly-work-skill 汇总本周周报`。
   - 目标：读取本地已存记录，生成当前周一到周日的 Word 周报。

3. `correct`
   - 触发：用户要求更正已存 weekly-work 记录。

4. `delete`
   - 触发：用户要求删除已存 weekly-work 记录。

5. `prompt`
   - 触发：`使用weekly-week-skill`、`使用weekly-week-skill给prompt` 或 `使用weekly-work-skill给prompt`。
   - These triggers select prompt, never collect; this mode is prompt-only and mutually exclusive with every other mode.

If the intent is unclear, ask which mode they want before acting.

## Shared Paths

Use these literals in shell examples:

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
SKILL_CREATOR=${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts
```

## collect

Collection rules:

- require/validate a single date or inclusive date range
- MM/DD uses current local year
- ask if missing/invalid
- supported examples: `7月1号`, `07/01`, `2026-07-01`, `7月1号到5号`, `07/01-07/05`, `2026-07-01至2026-07-05`
- do not store raw full chat, raw logs, or secrets
- do not generate Word during collection

Discovery:

- Run `scripts/project_history.py discover` to parse the requested date/range, resolve the current project root, and enumerate candidate threads from `state_5.sqlite`.
- The helper checks `${CODEX_HOME:-$HOME/.codex}/state_5.sqlite`, then `${CODEX_HOME:-$HOME/.codex}/sqlite/state_5.sqlite`.
- It returns only user-owned threads whose `cwd` is the project root or descendant cwd; same-prefix sibling directories are excluded.
- If discovery is unavailable, stop with an actionable error; do not fall back to the current chat.

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
TODAY="$(date +%F)"
python3 "$SKILL_DIR/scripts/project_history.py" discover \
  --cwd "$PWD" \
  --range "$USER_DATE_OR_RANGE" \
  --today "$TODAY" \
  --codex-home "${CODEX_HOME:-$HOME/.codex}" \
  --timezone Asia/Shanghai
```

Thread reading and extraction:

- For every discovered thread, call `codex_app__read_thread` with `includeOutputs: true`, `maxOutputCharsPerItem: 5000`, and a bounded turnLimit; follow `nextCursor` until `hasMore is false`.
- If `codex_app__read_thread` is unavailable or not exposed, run `scripts/project_history.py export` for each discovered thread ID; this helper is the only rollout fallback.
- In fallback mode, do not ask the user to provide the date's work content, do not manually collect from user-provided summaries, and do not write records unless supported facts are extracted from the exported project history.
- do not read or parse rollout JSONL directly or with ad hoc parsing; use `codex_app__read_thread` or `scripts/project_history.py export` only.
- Filter turns by `startedAt` converted to `Asia/Shanghai` local calendar dates.
- Never use thread `createdAt` as the work date.
- exclude collection-control messages, reminders, report commands, repeated status narration, failed attempts without useful findings, unrelated conversation, and internal review/subagent content.
- read user messages and Codex agent messages; include command/file evidence only when it concisely supports a completed action, result, artifact, or unresolved issue.
- do not copy raw logs, secrets, credentials, tokens, or unrelated personal information.
- Classify only supported facts into `daily_work`, `literature`, `research_progress`, `research_outputs`, and `unresolved`.
- group supported facts by source turn date; write one normalized payload per non-empty date.

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
THREAD_ID="thread-id-from-discover"
START_DATE="2026-07-01"
END_DATE="2026-07-05"
python3 "$SKILL_DIR/scripts/project_history.py" export \
  --thread-id "$THREAD_ID" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --codex-home "${CODEX_HOME:-$HOME/.codex}" \
  --timezone Asia/Shanghai \
  --max-chars 5000
```

Normalize each payload to JSON with these fields:

```json
{
  "date": "YYYY-MM-DD",
  "project": "current project directory name",
  "ai_source": "Codex",
  "daily_work": [],
  "literature": [],
  "research_progress": [],
  "research_outputs": [],
  "unresolved": []
}
```

The arrays must contain only facts supported by the project history in the requested period. Do not infer missing work or success.

Storage:

- Build all daily payloads in memory before writing.
- Skip dates with no supported facts.
- Use `weekly_store.py add` through JSON stdin with `--root` and `--today`.
- If the entire range has no supported content, write nothing and report: `No recordable project chat content found for the requested dates.`

Example:

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
TODAY="$(date +%F)"
python3 "$SKILL_DIR/scripts/weekly_store.py" add --root "$ROOT" --today "$TODAY" <<'JSON'
{"date":"2026-07-01","project":"codex","ai_source":"Codex","daily_work":["Implemented storage"],"literature":[],"research_progress":[],"research_outputs":[],"unresolved":[]}
JSON
```

Inspect the JSON status:

- `added` or `duplicate` -> reply exactly 收到 and nothing else
- `error` -> do not claim receipt
- Every daily `add` result must be `added` or `duplicate` before replying exactly 收到.
- Any `error` result -> do not claim receipt.

On success, keep the response to 收到 only: no headings, path, summary, or reminder.

## summarize

Summary rules:

- use local invocation date for current Monday-Sunday
- use the codex_app workspace dependency loader for this invocation
- call tool codex_app__load_workspace_dependencies with empty object; read returned Python executable; set BUNDLED_PYTHON to that exact path for this invocation
- BUNDLED_PYTHON="<Python executable returned by codex_app__load_workspace_dependencies>" for this invocation only; replace the placeholder with the tool result, do not run it literally
- use BUNDLED_PYTHON for report.py
- Use $BUNDLED_PYTHON for report.py and quick_validate after replacing the placeholder with the tool result.
- never install/use system Python for report.py
- No data means no empty report
- return the absolute Word path on success

Render the fixed report sections in this exact order:

1. 每日工作清单
2. 阅读文献情况
3. 研究进展情况
4. 科研成果情况

Example workflow:

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
REPORT_TEMPLATE="$SKILL_DIR/assets/weekly-report-template.docx"
# call tool codex_app__load_workspace_dependencies with empty object; read returned Python executable; set BUNDLED_PYTHON to that exact path for this invocation
BUNDLED_PYTHON="<Python executable returned by codex_app__load_workspace_dependencies>"
"$BUNDLED_PYTHON" "$SKILL_DIR/scripts/report.py" generate --root "$ROOT" --week-of "$(date +%F)" --template "$REPORT_TEMPLATE"
```

return absolute Word path on success

## correct

Correct mode:

- list relevant week first
- If multiple records match, ask and stop
- require exactly one record
- ask if ambiguous
- replace with complete normalized payload
- preserve others
- report concise result

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
TARGET_DATE="2026-06-30"
TODAY="$TARGET_DATE"
RECORD_ID="0123456789abcdef"
python3 "$SKILL_DIR/scripts/weekly_store.py" list --root "$ROOT" --week-of "$TARGET_DATE"
# Codex substitutes the confirmed illustrative value after the list result, then mutates only the chosen record.
python3 "$SKILL_DIR/scripts/weekly_store.py" replace --root "$ROOT" --today "$TODAY" "$RECORD_ID" <<'JSON'
{"date":"2026-06-30","project":"alpha","ai_source":"codex","daily_work":["Implemented storage"],"literature":[],"research_progress":[],"research_outputs":[],"unresolved":[]}
JSON
```

When the target is not unique, stop and ask for clarification before writing.

## delete

Delete mode:

- list relevant week first
- If multiple records match, ask and stop
- require exactly one record
- ask if ambiguous
- delete only explicit target
- preserve others
- report concise result

```bash
ROOT=$HOME/Documents/周报
SKILL_DIR=${CODEX_HOME:-$HOME/.codex}/skills/weekly-work-skill
TARGET_DATE="2026-06-30"
RECORD_ID="0123456789abcdef"
python3 "$SKILL_DIR/scripts/weekly_store.py" list --root "$ROOT" --week-of "$TARGET_DATE"
# Codex substitutes the confirmed illustrative value after the list result, then deletes only the chosen record.
python3 "$SKILL_DIR/scripts/weekly_store.py" delete --root "$ROOT" --week-of "$TARGET_DATE" "$RECORD_ID"
```

Deletion should only touch the requested record; do not rewrite unrelated entries.

## prompt

- Read references/external-ai-prompt.md only when asked for a prompt for another AI; the three prompt triggers count as that request.
- Read references/external-ai-prompt.md and return its complete file content exactly.
- Return only that file content: no extra commentary or code fence.
- In this mode, do not persist weekly-report data, do not generate Word, and do not run reminder check.

The prompt cleans up素材 for a weekly report; it must not invent missing facts.

## Guardrails

- Reminder is global AGENTS responsibility; while Skill active never run reminder check.
- No README or auxiliary docs.
- Keep this file under 500 lines.
- For validation, use the direct unittest entrypoint and the bundled quick_validate script before shipping changes.
- Include exact CLI examples without a user/version-hardcoded bundled Python.

## Validation

Run the skill contract tests directly:

```bash
python3 "$SKILL_DIR/tests/test_skill_contract.py"
```

Run the broader test suite and quick validation after the contract passes.

```bash
SKILL_CREATOR=${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts
# Use the workspace dependency loader to resolve BUNDLED_PYTHON, then run:
BUNDLED_PYTHON="<Python executable returned by codex_app__load_workspace_dependencies>"
"$BUNDLED_PYTHON" "$SKILL_CREATOR/quick_validate.py" "$SKILL_DIR"
```

For quick_validate, set SKILL_CREATOR from CODEX_HOME fallback and invoke $BUNDLED_PYTHON after replacing the placeholder with the tool result.
