# codex-export

[English](./README.md) | [简体中文](./README.zh-CN.md)

把 Codex Desktop 的本地会话记录，导出成适合人类浏览和分享的 HTML 页面。它支持本地导出、批量归档，以及上传到 GitHub Gist 后生成可直接分享的预览链接。整体交互和结构参考 [simonw/claude-code-transcripts: Tools for publishing transcripts for Claude Code sessions](https://github.com/simonw/claude-code-transcripts)。

## 安装

安装：

```bash
uv tool install codex-export
```

然后运行：

```bash
codex-export --help
```

如果 `codex-export` 找不到，通常是因为 `~/.local/bin` 还没加到 `PATH`。可以执行：

```bash
uv tool update-shell
```

如果你是在本仓库里做开发，再使用：

```bash
git clone git@github.com:allojohn/codex-export.git
cd codex-export
uv sync
uv run codex-export --help
```

## 让 Agent 调用

可以直接把下面这段话复制给 agent：

> Read `https://github.com/allojohn/codex-export/blob/main/README.md` and use it to operate `codex-export` for me. First ask which Codex session I want to export. Then ask whether I want local HTML files, a shareable GitHub Gist preview link, a full archive, or the original session JSONL copied into the output directory. Next, run the correct command and tell me the output path or share link.

## 分享优先

`codex-export` 最适合拿来做两件事：

1. 把本地 Codex 对话导出成可浏览的 HTML
2. 一键上传到 GitHub Gist，拿到可直接分享的预览链接

最常用的分享命令：

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --gist
```

或者先从本地最近会话里挑一个，再直接上传：

```bash
codex-export --gist
```

执行成功后会输出：

- Gist 地址
- `gisthost.github.io` 预览链接

这样别人不需要本地运行工具，也能直接打开你导出的页面。

## 常用命令

直接从本地最近会话里挑选：

```bash
codex-export
codex-export local
```

转换指定会话文件：

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl -o ./output
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --open
```

也支持从 URL 直接拉取 JSON/JSONL：

```bash
codex-export json https://example.com/session.jsonl -o ./output
```

上传到 GitHub Gist 并拿到预览链接：

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --gist
codex-export --gist
```

批量生成归档：

```bash
codex-export all -o ./codex-archive
codex-export all --dry-run
```

如果没有传 `-o/--output`，`local` 和 `json` 会默认写到临时目录；当没有传 `-o`、没有启用 `--gist`、也没有启用 `-a/--output-auto` 时，会自动打开结果页。

## 命令

- `local` / 默认命令：从 `~/.codex/sessions` 读取最近本地会话
- `json`：转换指定 `.json` / `.jsonl` 文件，或远程 URL
- `all`：转换本地全部会话，生成可浏览 archive

Codex 没有对应 Claude 项目里的 `web` 会话 API 导入能力，所以这里没有实现 `web` 子命令。

## 输出选项

`local` 和 `json` 支持这些选项：

- `-o, --output DIRECTORY`：输出目录；默认写到临时目录
- `-a, --output-auto`：自动按 session 文件名创建子目录
- `--source PATH`：仅 `local` 支持，自定义本地会话目录
- `--limit INTEGER`：仅 `local` 支持，控制交互列表里展示多少个最近会话
- `--open`：生成后打开 `index.html`
- `--gist`：上传 HTML 到 GitHub Gist 并输出 `gisthost.github.io` 预览链接
- `--json`：把原始 session 文件一起复制到输出目录

生成结果的页面结构是：

- `index.html`：索引页，只展示每个 turn 的 user 摘要
- `page-001.html`、`page-002.html` ...：完整消息分页，包含 assistant、tool call、tool result、commentary 等完整内容

## 批量归档

`all` 命令会生成三层结构：

- 总索引页：列出所有 workspace
- 每个 workspace 的索引页：列出该 workspace 下的 sessions
- 每个 session 的 export 页面

默认会排除 agent / subagent sessions；只有加上 `--include-agents` 才会把这些会话也纳入归档。

`all` 支持这些选项：

- `--source DIRECTORY`：源目录，默认 `~/.codex/sessions`
- `-o, --output DIRECTORY`：输出目录，默认 `./codex-archive`
- `--include-agents`：包含 agent / subagent sessions
- `--dry-run`：只预览会转换哪些内容，不写文件
- `--open`：生成后打开 archive 首页
- `-q, --quiet`：安静模式，只输出错误

## 当前支持

- `local`：交互式选择最近本地会话
- `json`：转换本地或远程 `.json` / `.jsonl`
- `all`：按 workspace 分组生成 archive
- `-a / --output-auto`
- `--json`
- `--open`
- `--gist`：上传生成的 HTML 到 GitHub Gist，并输出 `gisthost.github.io` 预览链接，适合直接分享
- 多页转写：按 Codex turn 分页
- Gist 预览修复：自动注入相对链接修复脚本，保证分页和锚点在 `gisthost.github.io` 下可用

## 已知限制

- `local` / `all` 目前只扫描 Codex 本地 `.jsonl` 会话目录，不读 SQLite 历史库
- `reasoning.encrypted_content` 不会解密，只会显示可见 summary
- 一些 `event_msg` 是 `response_item` 的重复镜像，页面里会去重并优先展示最终可读消息
- `--gist` 依赖 `gh` 已安装且完成 `gh auth login`
- `web` 子命令未实现，因为目前没有对应的 Codex Web session 导入接口
