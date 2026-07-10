# weekly-work-skill

Codex 周报撰写 skill：按日期从当前项目的 Codex 历史对话中提取工作内容，保存为结构化记录，并在需要时生成 Word 周报。

## 功能

- 按日期或日期范围提取项目工作内容
- 汇总指定日期所在周的周报
- 生成 `.docx` Word 文档
- 支持查看给其他 AI 使用的周报素材整理 prompt
- 支持更正、删除已保存记录
- 支持 macOS、Linux、Windows

## 安装

在 Codex 中安装 GitHub skill：

```text
安装 https://github.com/xjchen1/weekly-work-skill
常用提示词
提取某一天的工作内容：
使用weekly-work-skill,提取5月26日工作内容
提取一段日期范围：
使用weekly-work-skill,提取5月26日至5月31日工作内容
汇总某一天所在周的周报：
使用weekly-work-skill,汇总5月26日那一周的工作内容
获取给 ChatGPT、Claude、Gemini 等其他 AI 使用的整理 prompt：
使用weekly-work-skill,给prompt
工作方式
提取时，你只需要给日期。
例如：
使用weekly-work-skill,提取7月1日工作内容
skill 会尝试从当前项目对应日期的 Codex 历史对话中查找可写入周报的事实，并保存记录。
汇总时：
使用weekly-work-skill,汇总7月1日那一周的工作内容
skill 会读取本地已保存记录，并生成该日期所在周的 Word 周报。
周报模板
生成的周报包含四个固定部分：
每日工作清单
阅读文献情况
研究进展情况
科研成果情况
数据保存位置
默认保存到：
~/Documents/周报
其中结构化记录通常位于：
~/Documents/周报/data/
生成的 Word 周报也会保存在周报目录下。
Windows 说明
该 skill 已处理 Windows 兼容：
不依赖 Unix-only 的 fcntl
Windows 下使用 msvcrt 做文件锁
文档示例不硬编码 python3
测试使用当前 Python 解释器运行
如果 Windows 端中文显示乱码，通常是终端或编辑器编码问题。仓库文件本身使用 UTF-8。
