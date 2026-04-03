# codex-export

[English](./README.md) | [简体中文](./README.zh-CN.md)

Export Codex Desktop session records as browsable, shareable HTML pages. `codex-export` supports local exports, batch archives, and GitHub Gist uploads with ready-to-share preview links. The overall interaction model is inspired by [simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts), but the parser is adapted to the real Codex session format.

## Install

Install from PyPI:

```bash
uv tool install codex-export
```

Then run:

```bash
codex-export --help
```

If `codex-export` is not found, you may need to add `~/.local/bin` to your `PATH`:

```bash
uv tool update-shell
```

For local development:

```bash
git clone git@github.com:allojohn/codex-export.git
cd codex-export
uv sync
uv run codex-export --help
```

## For Agents

You can give this prompt to an agent:

> Read `https://github.com/allojohn/codex-export/blob/main/README.md` and use it to operate `codex-export` for me. First ask which Codex session I want to export. Then ask whether I want local HTML files, a shareable GitHub Gist preview link, a full archive, or the original session JSONL copied into the output directory. Next, run the correct command and tell me the output path or share link.

## Share First

`codex-export` is especially useful for two things:

1. Exporting a Codex conversation into browsable HTML
2. Uploading it to GitHub Gist and getting a shareable preview link

Most common sharing command:

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --gist
```

Or pick a recent local session and upload it directly:

```bash
codex-export --gist
```

On success, the command prints:

- The Gist URL
- A `gisthost.github.io` preview URL

That means other people can open the exported page directly without running the tool locally.

## Common Commands

Pick from recent local sessions:

```bash
codex-export
codex-export local
```

Export a specific session file:

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl -o ./output
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --open
```

Export from a remote JSON/JSONL URL:

```bash
codex-export json https://example.com/session.jsonl -o ./output
```

Upload to GitHub Gist and get a preview link:

```bash
codex-export json ~/.codex/sessions/2026/03/31/rollout-xxxx.jsonl --gist
codex-export --gist
```

Generate a full archive:

```bash
codex-export all -o ./codex-archive
codex-export all --dry-run
```

If you do not pass `-o/--output`, both `local` and `json` write to a temporary directory by default. If you do not pass `-o`, do not enable `--gist`, and do not enable `-a/--output-auto`, the result page opens automatically.

## Commands

- `local` or the default command: read recent local sessions from `~/.codex/sessions`
- `json`: export a specific `.json` / `.jsonl` file, or a remote URL
- `all`: export all local sessions into a browsable archive

Codex does not currently have an equivalent of the Claude project's `web` session import API, so this project does not implement a `web` command.

## Output Options

`local` and `json` support:

- `-o, --output DIRECTORY`: output directory; defaults to a temporary directory
- `-a, --output-auto`: automatically create a subdirectory based on the session filename
- `--source PATH`: `local` only, override the local session directory
- `--limit INTEGER`: `local` only, control how many recent sessions appear in the picker
- `--open`: open `index.html` after export
- `--gist`: upload HTML to GitHub Gist and print a `gisthost.github.io` preview link
- `--json`: copy the original session file into the output directory

Generated output is structured like this:

- `index.html`: index page with one summary entry per turn
- `page-001.html`, `page-002.html`, ...: paginated full content including assistant messages, tool calls, tool results, commentary, and more

## Batch Archive

The `all` command generates a three-level archive:

- A top-level index of all workspaces
- One index page per workspace
- One export page per session

Agent and subagent sessions are excluded by default. Add `--include-agents` to include them.

`all` supports:

- `--source DIRECTORY`: source directory, default `~/.codex/sessions`
- `-o, --output DIRECTORY`: output directory, default `./codex-archive`
- `--include-agents`: include agent and subagent sessions
- `--dry-run`: show what would be exported without writing files
- `--open`: open the archive index after generation
- `-q, --quiet`: only print errors

## Current Features

- `local`: interactive picker for recent local sessions
- `json`: export local or remote `.json` / `.jsonl`
- `all`: generate a workspace-grouped archive
- `-a / --output-auto`
- `--json`
- `--open`
- `--gist`: upload generated HTML to GitHub Gist and print a shareable preview link
- Multi-page exports: paginated by Codex turn
- Gist preview fixups: automatically inject relative-link fixes for `gisthost.github.io`

## Limitations

- `local` and `all` currently scan Codex local `.jsonl` session directories only; they do not read the SQLite history store
- `reasoning.encrypted_content` is not decrypted; only visible summaries are shown
- Some `event_msg` items mirror `response_item` entries; the rendered page de-duplicates these and prefers the final readable form
- `--gist` requires `gh` to be installed and authenticated with `gh auth login`
- No `web` command is implemented because Codex does not currently expose a corresponding web-session import interface
