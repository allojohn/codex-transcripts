"""Convert Codex Desktop session JSON and JSONL files into browseable HTML transcripts."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen
from html import escape
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from jinja2 import Environment, PackageLoader
import markdown
import questionary

PROMPTS_PER_PAGE = 5
LONG_TEXT_THRESHOLD = 300
COMMIT_PATTERN = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)")

_jinja_env = Environment(
    loader=PackageLoader("codex_transcripts", "templates"),
    autoescape=True,
)
_macros_template = _jinja_env.get_template("macros.html")
_macros = _macros_template.module


@dataclass
class SessionInfo:
    path: Path
    session_id: str
    thread_name: str
    updated_at: str
    summary: str
    cwd: str
    workspace_name: str
    size: int
    is_agent: bool = False


@dataclass
class TranscriptEntry:
    timestamp: str
    role_class: str
    role_label: str
    content_html: str
    index_text: str = ""


@dataclass
class CommitEvent:
    timestamp: str
    commit_hash: str
    commit_message: str
    page_anchor: str = ""


@dataclass
class Turn:
    turn_id: str
    started_at: str
    entries: list[TranscriptEntry] = field(default_factory=list)
    prompt_preview: str = "(no prompt)"
    tool_calls: int = 0
    long_texts: list[str] = field(default_factory=list)
    commits: list[CommitEvent] = field(default_factory=list)
    status: str = "in_progress"
    completed_at: str | None = None


def get_template(name: str):
    return _jinja_env.get_template(name)


def render_markdown_text(text: str) -> str:
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def format_json(obj: Any) -> str:
    try:
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
    except TypeError:
        formatted = str(obj)
    return f"<pre class=\"json\">{escape(formatted)}</pre>"


def make_msg_id(timestamp: str) -> str:
    return f"msg-{timestamp.replace(':', '-').replace('.', '-')}"


def read_session_index(index_path: Path) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    if not index_path.exists():
        return results
    with index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = item.get("id")
            if session_id:
                results[session_id] = item
    return results


def extract_session_meta(filepath: Path) -> dict[str, Any]:
    with filepath.open("r", encoding="utf-8") as handle:
        first_line = handle.readline().strip()
    if not first_line:
        return {}
    try:
        item = json.loads(first_line)
    except json.JSONDecodeError:
        return {}
    if item.get("type") != "session_meta":
        return {}
    return item.get("payload", {})


def session_summary_from_file(filepath: Path) -> str:
    meta = extract_session_meta(filepath)
    if meta.get("cwd"):
        originator = meta.get("originator") or "Codex"
        return f"{originator} · {Path(meta['cwd']).name}"
    return filepath.stem


def is_agent_session_meta(meta: dict[str, Any]) -> bool:
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True
    if meta.get("agent_role") or meta.get("agent_nickname"):
        return True
    return False


def workspace_name_from_meta(meta: dict[str, Any]) -> str:
    cwd = meta.get("cwd", "")
    if cwd:
        return Path(cwd).name or "unknown-workspace"
    return "unknown-workspace"


def find_local_sessions(
    source_dir: Path,
    limit: int = 10,
    include_agents: bool = False,
) -> list[SessionInfo]:
    index = read_session_index(Path.home() / ".codex" / "session_index.jsonl")
    results: list[SessionInfo] = []
    for path in source_dir.glob("**/*.jsonl"):
        meta = extract_session_meta(path)
        is_agent = is_agent_session_meta(meta)
        if is_agent and not include_agents:
            continue
        session_id = meta.get("id") or path.stem.split("-")[-1]
        indexed = index.get(session_id, {})
        stat = path.stat()
        results.append(
            SessionInfo(
                path=path,
                session_id=session_id,
                thread_name=indexed.get("thread_name") or path.stem,
                updated_at=indexed.get("updated_at") or datetime.fromtimestamp(stat.st_mtime).isoformat(),
                summary=session_summary_from_file(path),
                cwd=meta.get("cwd", ""),
                workspace_name=workspace_name_from_meta(meta),
                size=stat.st_size,
                is_agent=is_agent,
            )
        )
    results.sort(
        key=lambda item: _timestamp_sort_key(item.updated_at, item.path.stat().st_mtime),
        reverse=True,
    )
    return results[:limit]


def find_all_sessions(
    source_dir: Path,
    include_agents: bool = False,
) -> list[dict[str, Any]]:
    sessions = find_local_sessions(source_dir, limit=100000, include_agents=include_agents)
    grouped: dict[str, dict[str, Any]] = {}
    for session in sessions:
        key = session.workspace_name
        if key not in grouped:
            grouped[key] = {
                "name": key,
                "sessions": [],
            }
        grouped[key]["sessions"].append(session)

    projects = list(grouped.values())
    for project in projects:
        project["sessions"].sort(
            key=lambda item: _timestamp_sort_key(item.updated_at, item.path.stat().st_mtime),
            reverse=True,
        )
    projects.sort(
        key=lambda item: _timestamp_sort_key(
            item["sessions"][0].updated_at,
            item["sessions"][0].path.stat().st_mtime,
        ) if item["sessions"] else 0,
        reverse=True,
    )
    return projects


def _timestamp_sort_key(value: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def render_response_message(payload: dict[str, Any]) -> tuple[str, str, str]:
    role = payload.get("role", "assistant")
    blocks = payload.get("content", [])
    rendered: list[str] = []
    index_parts: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in {"input_text", "output_text", "text"}:
            text = block.get("text", "")
            if text:
                rendered.append(render_markdown_text(text))
                index_parts.append(text.strip())
        else:
            rendered.append(format_json(block))
    role_map = {
        "user": ("user", "User"),
        "assistant": ("assistant", "Assistant"),
        "developer": ("system", "Developer"),
        "system": ("system", "System"),
    }
    role_class, role_label = role_map.get(role, ("system", role.title()))
    return role_class, role_label, _wrap_user_content(role_class, "".join(rendered)), "\n".join(index_parts)


def _wrap_user_content(role_class: str, content_html: str) -> str:
    if role_class == "user":
        return _macros.user_content(content_html)
    if role_class == "assistant":
        return _macros.assistant_text(content_html)
    return _macros.meta_block(content_html)


def render_function_call(payload: dict[str, Any]) -> tuple[str, str]:
    name = payload.get("name", "Tool")
    arguments = payload.get("arguments", "")
    return (
        _macros.tool_use(name, "", arguments or "{}", ""),
        f"{name} {arguments}".strip(),
    )


def render_custom_tool_call(payload: dict[str, Any]) -> tuple[str, str]:
    name = payload.get("name", "Custom Tool")
    tool_input = payload.get("input", "")
    return (
        _macros.tool_use(name, "", tool_input or "{}", ""),
        f"{name} {tool_input}".strip(),
    )


def render_function_call_output(payload: dict[str, Any]) -> tuple[str, str]:
    output = payload.get("output", "")
    return (
        _macros.tool_result(f"<pre>{escape(str(output))}</pre>", False, False),
        str(output),
    )


def render_custom_tool_call_output(payload: dict[str, Any]) -> tuple[str, str]:
    output = payload.get("output", "")
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = output
    else:
        parsed = output

    if isinstance(parsed, dict) and "output" in parsed:
        text = str(parsed["output"])
    else:
        text = str(parsed)
    return (
        _macros.tool_result(f"<pre>{escape(text)}</pre>", False, False),
        text,
    )


def extract_commit_events(text: str, timestamp: str) -> list[CommitEvent]:
    if not text:
        return []
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
    else:
        parsed = text

    if isinstance(parsed, dict) and "output" in parsed:
        text = str(parsed["output"])
    else:
        text = str(parsed)

    if "\n" not in text and any(token in text for token in ("\\r\\n", "\\n", "\\r")):
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")

    commits: list[CommitEvent] = []
    for line in text.splitlines():
        match = COMMIT_PATTERN.match(line.strip())
        if not match:
            continue
        commits.append(
            CommitEvent(
                timestamp=timestamp,
                commit_hash=match.group(1),
                commit_message=match.group(2).strip(),
            )
        )
    return commits


def render_reasoning(payload: dict[str, Any]) -> tuple[str, str]:
    summary = payload.get("summary") or []
    if not summary:
        return _macros.meta_block("<p>Reasoning captured privately.</p>"), ""
    text = "\n".join(
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in summary
    )
    return _macros.thinking(render_markdown_text(text)), text


def load_session_items(filepath: Path) -> list[dict[str, Any]]:
    if filepath.suffix == ".jsonl":
        items: list[dict[str, Any]] = []
        with filepath.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        return items

    with filepath.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "events", "entries", "loglines"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "type" in data:
            return [data]
    return []


def parse_session_file(filepath: Path) -> tuple[dict[str, Any], list[Turn]]:
    meta: dict[str, Any] = {}
    turns: list[Turn] = []
    current_turn: Turn | None = None

    for item in load_session_items(filepath):
        item_type = item.get("type")
        timestamp = item.get("timestamp", "")
        payload = item.get("payload", {})

        if item_type == "session_meta":
            meta = payload
            continue

        if item_type == "event_msg" and payload.get("type") == "task_started":
            if current_turn and current_turn.entries:
                turns.append(current_turn)
            current_turn = Turn(
                turn_id=payload.get("turn_id", f"turn-{len(turns) + 1}"),
                started_at=timestamp,
            )
            continue

        if current_turn is None:
            current_turn = Turn(turn_id="bootstrap", started_at=timestamp)

        if item_type == "response_item":
            payload_type = payload.get("type")
            if payload_type == "message":
                if should_skip_message(payload):
                    continue
                role_class, role_label, content_html, index_text = render_response_message(payload)
                if role_class == "assistant" and payload.get("phase") == "commentary":
                    role_class = "commentary"
                    role_label = "Commentary"
                current_turn.entries.append(
                    TranscriptEntry(
                        timestamp=timestamp,
                        role_class=role_class,
                        role_label=role_label,
                        content_html=content_html,
                        index_text=index_text,
                    )
                )
                if role_class == "user" and current_turn.prompt_preview == "(no prompt)" and index_text.strip():
                    current_turn.prompt_preview = first_line(index_text)
                if role_class == "assistant":
                    cleaned_text = index_text.strip()
                    if len(cleaned_text) >= LONG_TEXT_THRESHOLD:
                        current_turn.long_texts.append(cleaned_text)
            elif payload_type == "function_call":
                current_turn.tool_calls += 1
                content_html, index_text = render_function_call(payload)
                current_turn.entries.append(
                    TranscriptEntry(timestamp, "tool", "Tool Call", content_html, index_text)
                )
            elif payload_type == "function_call_output":
                content_html, index_text = render_function_call_output(payload)
                current_turn.entries.append(
                    TranscriptEntry(timestamp, "tool-reply", "Tool Result", content_html, index_text)
                )
                current_turn.commits.extend(extract_commit_events(index_text, timestamp))
            elif payload_type == "custom_tool_call":
                current_turn.tool_calls += 1
                content_html, index_text = render_custom_tool_call(payload)
                current_turn.entries.append(
                    TranscriptEntry(timestamp, "tool", "Tool Call", content_html, index_text)
                )
            elif payload_type == "custom_tool_call_output":
                content_html, index_text = render_custom_tool_call_output(payload)
                current_turn.entries.append(
                    TranscriptEntry(timestamp, "tool-reply", "Tool Result", content_html, index_text)
                )
                current_turn.commits.extend(extract_commit_events(index_text, timestamp))
            elif payload_type == "reasoning":
                content_html, index_text = render_reasoning(payload)
                current_turn.entries.append(
                    TranscriptEntry(timestamp, "thinking", "Reasoning", content_html, index_text)
                )
            continue

        if item_type == "event_msg":
            payload_type = payload.get("type")
            if payload_type in {"agent_message", "user_message", "token_count"}:
                continue
            if payload_type == "task_complete":
                current_turn.status = "completed"
                current_turn.completed_at = timestamp
                continue
            if payload_type == "turn_aborted":
                current_turn.status = "aborted"
                current_turn.completed_at = timestamp
                current_turn.entries.append(
                    TranscriptEntry(
                        timestamp=timestamp,
                        role_class="system",
                        role_label="Turn Aborted",
                        content_html=_macros.meta_block("<p>This turn was interrupted before completion.</p>"),
                        index_text="turn aborted",
                    )
                )
                continue
            if payload_type == "task_started":
                continue
            current_turn.entries.append(
                TranscriptEntry(
                    timestamp=timestamp,
                    role_class="system",
                    role_label=payload_type.replace("_", " ").title(),
                    content_html=_macros.meta_block(format_json(payload)),
                    index_text=payload_type,
                )
            )

    if current_turn and current_turn.entries:
        turns.append(current_turn)

    return meta, turns


def should_skip_message(payload: dict[str, Any]) -> bool:
    role = payload.get("role")
    if role == "developer":
        return True
    blocks = payload.get("content", [])
    text = "\n".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and "text" in block
    ).strip()
    hidden_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<app-context>",
        "<collaboration_mode>",
        "<apps_instructions>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<turn_aborted>",
    )
    return bool(text) and text.startswith(hidden_prefixes)


def first_line(text: str, max_length: int = 180) -> str:
    candidate = text.strip().splitlines()[0] if text.strip() else "(no prompt)"
    return candidate if len(candidate) <= max_length else candidate[: max_length - 3] + "..."


def render_entry(entry: TranscriptEntry) -> str:
    msg_id = make_msg_id(entry.timestamp)
    return _macros.message(
        entry.role_class,
        entry.role_label,
        msg_id,
        entry.timestamp,
        entry.content_html,
    )


def build_pagination(current_page: int, total_pages: int) -> str:
    return _macros.pagination(current_page, total_pages)


def build_index_pagination(total_pages: int) -> str:
    return _macros.index_pagination(total_pages)


def build_index_items(turns: list[Turn]) -> list[str]:
    timeline: list[tuple[float, int, str]] = []
    for idx, turn in enumerate(turns, start=1):
        page_num = math.ceil(idx / PROMPTS_PER_PAGE)
        first_entry_timestamp = turn.entries[0].timestamp if turn.entries else turn.started_at
        long_texts_html = "".join(
            _macros.index_long_text(render_markdown_text(text)) for text in turn.long_texts
        )
        stats_html = _macros.index_stats(
            f"{turn.tool_calls} tool calls · {turn.status}",
            long_texts_html,
        )
        timeline.append(
            (
                _timestamp_sort_key(turn.started_at, idx),
                0,
                _macros.index_item(
                    idx,
                    f"page-{page_num:03d}.html#{make_msg_id(first_entry_timestamp)}",
                    turn.started_at,
                    render_markdown_text(turn.prompt_preview),
                    stats_html,
                ),
            )
        )
        for commit_offset, commit in enumerate(turn.commits):
            timeline.append(
                (
                    _timestamp_sort_key(commit.timestamp, idx + (commit_offset / 1000.0)),
                    1,
                    _macros.index_commit(
                        commit.commit_hash,
                        commit.commit_message,
                        commit.timestamp,
                        commit.page_anchor or f"page-{page_num:03d}.html#turn-{idx}",
                    ),
                )
            )
    timeline.sort(key=lambda item: (item[0], item[1]))
    return [item_html for _, _, item_html in timeline]


def build_search_data(turns: list[Turn]) -> list[dict[str, str]]:
    search_data: list[dict[str, str]] = []
    for idx, turn in enumerate(turns, start=1):
        page_num = math.ceil(idx / PROMPTS_PER_PAGE)
        page_file = f"page-{page_num:03d}.html"
        for entry in turn.entries:
            search_text = entry.index_text.strip()
            if not search_text:
                continue
            msg_id = make_msg_id(entry.timestamp)
            search_data.append(
                {
                    "page": page_file,
                    "anchor": msg_id,
                    "role": entry.role_label,
                    "timestamp": entry.timestamp,
                    "text": search_text,
                }
            )
    return search_data


def serialize_search_data(turns: list[Turn]) -> str:
    return (
        json.dumps(build_search_data(turns), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def generate_html(input_path: Path, output_dir: Path) -> dict[str, Any]:
    meta, turns = parse_session_file(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    template_index = get_template("index.html")
    template_page = get_template("page.html")

    total_pages = max(1, math.ceil(len(turns) / PROMPTS_PER_PAGE))
    index_items = build_index_items(turns)

    all_messages_html = "".join(
        _macros.turn_section(
            idx,
            turn.prompt_preview,
            turn.status,
            "".join(render_entry(entry) for entry in turn.entries),
        )
        for idx, turn in enumerate(turns, start=1)
    )

    index_html = template_index.render(
        css=CSS,
        js=JS,
        transcript_title=build_transcript_title(meta, input_path),
        session_meta=build_session_meta(meta),
        prompt_num=len(turns),
        total_messages=sum(len(turn.entries) for turn in turns),
        total_tool_calls=sum(turn.tool_calls for turn in turns),
        total_commits=sum(len(turn.commits) for turn in turns),
        total_pages=total_pages,
        pagination_html=build_index_pagination(total_pages),
        index_items_html="".join(index_items),
        all_messages_html=all_messages_html,
        search_data_json=serialize_search_data(turns),
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    for page_num in range(1, total_pages + 1):
        start = (page_num - 1) * PROMPTS_PER_PAGE
        end = start + PROMPTS_PER_PAGE
        turns_for_page = turns[start:end]
        messages_html = "".join(
            _macros.turn_section(
                start + offset + 1,
                turn.prompt_preview,
                turn.status,
                "".join(render_entry(entry) for entry in turn.entries),
            )
            for offset, turn in enumerate(turns_for_page)
        )
        page_html = template_page.render(
            css=CSS,
            js=JS,
            transcript_title=build_transcript_title(meta, input_path),
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=build_pagination(page_num, total_pages),
            messages_html=messages_html,
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(page_html, encoding="utf-8")

    return {"meta": meta, "turns": turns, "output_dir": output_dir}


def build_transcript_title(meta: dict[str, Any], input_path: Path) -> str:
    index = read_session_index(Path.home() / ".codex" / "session_index.jsonl")
    session_id = meta.get("id")
    if session_id and session_id in index:
        return index[session_id].get("thread_name") or input_path.stem
    return input_path.stem


def build_session_meta(meta: dict[str, Any]) -> list[dict[str, str]]:
    cwd = meta.get("cwd", "")
    workspace_name = Path(cwd).name if cwd else ""
    fields = [
        ("Session ID", meta.get("id", "")),
        ("Originator", meta.get("originator", "")),
        ("Model Provider", meta.get("model_provider", "")),
        ("CLI Version", meta.get("cli_version", "")),
        ("Workspace", workspace_name),
    ]
    return [{"label": label, "value": value} for label, value in fields if value]


def generate_batch_html(
    source_folder: Path,
    output_dir: Path,
    include_agents: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    projects = find_all_sessions(source_folder, include_agents=include_agents)
    total_sessions = sum(len(project["sessions"]) for project in projects)

    if dry_run:
        return {
            "projects": projects,
            "total_projects": len(projects),
            "total_sessions": total_sessions,
            "output_dir": output_dir,
            "failed_sessions": [],
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    successful_projects: list[dict[str, Any]] = []
    failed_sessions: list[dict[str, str]] = []
    successful_sessions = 0

    for project in projects:
        project_dir = output_dir / project["name"]
        project_dir.mkdir(parents=True, exist_ok=True)
        successful_project_sessions: list[SessionInfo] = []
        for session in project["sessions"]:
            session_dir = project_dir / session.path.stem
            try:
                generate_html(session.path, session_dir)
            except Exception as exc:
                failed_sessions.append(
                    {
                        "project": project["name"],
                        "session": session.path.stem,
                        "error": str(exc),
                    }
                )
                continue
            successful_project_sessions.append(session)
            successful_sessions += 1

        if not successful_project_sessions:
            continue

        successful_projects.append(
            {
                "name": project["name"],
                "sessions": successful_project_sessions,
            }
        )

        project_html = get_template("project_index.html").render(
            css=CSS,
            js=JS,
            project_name=project["name"],
            sessions=successful_project_sessions,
            session_count=len(successful_project_sessions),
        )
        (project_dir / "index.html").write_text(project_html, encoding="utf-8")

    archive_html = get_template("master_index.html").render(
        css=CSS,
        js=JS,
        projects=successful_projects,
        total_projects=len(successful_projects),
        total_sessions=successful_sessions,
    )
    (output_dir / "index.html").write_text(archive_html, encoding="utf-8")
    return {
        "projects": successful_projects,
        "total_projects": len(successful_projects),
        "total_sessions": successful_sessions,
        "output_dir": output_dir,
        "failed_sessions": failed_sessions,
    }


def default_output_dir(input_path: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="codex-transcript-"))
    return temp_dir / input_path.stem


def output_auto_dir(parent: Path | None, name: str) -> Path:
    return (parent or Path(".")) / name


def copy_source_json(session_file: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / session_file.name
    shutil.copy(session_file, destination)
    return destination


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def fetch_url_to_tempfile(url: str) -> tuple[Path, str]:
    try:
        with urlopen(url) as response:
            body = response.read()
    except HTTPError as exc:
        raise click.ClickException(f"Failed to fetch URL: {exc.code} {exc.reason}")
    except URLError as exc:
        raise click.ClickException(f"Failed to fetch URL: {exc.reason}")

    parsed = urlparse(url)
    stem = Path(parsed.path).stem or "session"
    suffix = ".jsonl" if parsed.path.endswith(".jsonl") else ".json"
    temp_dir = Path(tempfile.mkdtemp(prefix="codex-transcript-url-"))
    temp_file = temp_dir / f"{stem}{suffix}"
    temp_file.write_bytes(body)
    return temp_file, stem


GIST_PREVIEW_JS = r"""
(function() {
  var hostname = window.location.hostname;
  if (hostname !== 'gisthost.github.io' && hostname !== 'gistpreview.github.io') return;
  var match = window.location.search.match(/^\?([^/]+)/);
  if (!match) return;
  var gistId = match[1];

  function rewriteLinks(root) {
    (root || document).querySelectorAll('a[href]').forEach(function(link) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('?')) return;
      if (href.startsWith('http') || href.startsWith('#') || href.startsWith('//')) return;
      var parts = href.split('#');
      var filename = parts[0];
      var anchor = parts.length > 1 ? '#' + parts[1] : '';
      link.setAttribute('href', '?' + gistId + '/' + filename + anchor);
    });
  }

  rewriteLinks();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { rewriteLinks(); });
  }

  var observer = new MutationObserver(function(mutations) {
    mutations.forEach(function(mutation) {
      mutation.addedNodes.forEach(function(node) {
        if (node.nodeType === 1) {
          rewriteLinks(node);
          if (node.tagName === 'A' && node.getAttribute('href')) {
            var href = node.getAttribute('href');
            if (!href.startsWith('?') && !href.startsWith('http') &&
                !href.startsWith('#') && !href.startsWith('//')) {
              var parts = href.split('#');
              var filename = parts[0];
              var anchor = parts.length > 1 ? '#' + parts[1] : '';
              node.setAttribute('href', '?' + gistId + '/' + filename + anchor);
            }
          }
        }
      });
    });
  });

  function startObserving() {
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    } else {
      setTimeout(startObserving, 10);
    }
  }
  startObserving();

  function scrollToFragment() {
    var hash = window.location.hash;
    if (!hash) return false;
    var targetId = hash.substring(1);
    var target = document.getElementById(targetId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return true;
    }
    return false;
  }

  if (!scrollToFragment()) {
    [100, 300, 500, 1000, 2000].forEach(function(delay) {
      setTimeout(scrollToFragment, delay);
    });
  }
})();
"""


def inject_gist_preview_js(output_dir: Path) -> None:
    for html_file in Path(output_dir).glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        if "</body>" in content and GIST_PREVIEW_JS not in content:
            content = content.replace(
                "</body>", f"<script>{GIST_PREVIEW_JS}</script>\n</body>"
            )
            html_file.write_text(content, encoding="utf-8")


def create_gist(output_dir: Path, public: bool = False) -> tuple[str, str]:
    html_files = list(Path(output_dir).glob("*.html"))
    if not html_files:
        raise click.ClickException("No HTML files found to upload to gist.")

    cmd = ["gh", "gist", "create"]
    cmd.extend(str(path) for path in sorted(html_files))
    if public:
        cmd.append("--public")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        error_msg = exc.stderr.strip() if exc.stderr else str(exc)
        raise click.ClickException(f"Failed to create gist: {error_msg}")
    except FileNotFoundError:
        raise click.ClickException(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )

    gist_url = result.stdout.strip()
    gist_id = gist_url.rstrip("/").split("/")[-1]
    return gist_id, gist_url


def pick_local_session(source_dir: Path, limit: int) -> SessionInfo | None:
    sessions = find_local_sessions(source_dir, limit=limit, include_agents=False)
    if not sessions:
        return None
    choice_map = {
        f"{session.updated_at[:16]}  {session.thread_name}  [{session.summary}]": session
        for session in sessions
    }
    selection = questionary.select("Choose a Codex session", choices=list(choice_map.keys())).ask()
    if not selection:
        return None
    return choice_map[selection]


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(local)


@cli.command()
@click.option("--source", type=click.Path(path_type=Path), default=Path.home() / ".codex" / "sessions")
@click.option("--limit", type=int, default=10)
@click.option("-o", "--output", type=click.Path(path_type=Path))
@click.option("-a", "--output-auto", is_flag=True, help="Auto-name output subdirectory based on session filename.")
@click.option("--gist", is_flag=True, help="Upload to GitHub Gist and output a gisthost.github.io URL.")
@click.option("--json", "include_json", is_flag=True, help="Include the original session file in the output directory.")
@click.option("--open", "should_open", is_flag=True, help="Open the generated index.html in your default browser.")
def local(
    source: Path,
    limit: int,
    output: Path | None,
    output_auto: bool,
    gist: bool,
    include_json: bool,
    should_open: bool,
) -> None:
    session = pick_local_session(source, limit)
    if session is None:
        raise click.ClickException("No Codex sessions found.")
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        destination = output_auto_dir(output, session.path.stem)
    else:
        destination = output or default_output_dir(session.path)
        if output and output.suffix:
            destination = output.parent / output.stem
    if destination.exists() and destination.is_file():
        raise click.ClickException("Output must be a directory.")
    result = generate_html(session.path, destination)
    index_path = result["output_dir"] / "index.html"
    click.echo(f"Transcript: {index_path}")
    if include_json:
        json_dest = copy_source_json(session.path, result["output_dir"])
        click.echo(f"JSON: {json_dest} ({json_dest.stat().st_size / 1024:.1f} KB)")
    if gist:
        inject_gist_preview_js(result["output_dir"])
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(result["output_dir"])
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")
    if should_open or auto_open:
        webbrowser.open(index_path.as_uri())


@cli.command(name="json")
@click.argument("session_file")
@click.option("-o", "--output", type=click.Path(path_type=Path))
@click.option("-a", "--output-auto", is_flag=True, help="Auto-name output subdirectory based on filename.")
@click.option("--gist", is_flag=True, help="Upload to GitHub Gist and output a gisthost.github.io URL.")
@click.option("--json", "include_json", is_flag=True, help="Include the original JSON session file in the output directory.")
@click.option("--open", "should_open", is_flag=True, help="Open the generated index.html in your default browser.")
def json_command(
    session_file: str,
    output: Path | None,
    output_auto: bool,
    gist: bool,
    include_json: bool,
    should_open: bool,
) -> None:
    if is_url(session_file):
        click.echo(f"Fetching {session_file}...")
        resolved_file, derived_name = fetch_url_to_tempfile(session_file)
    else:
        resolved_file = Path(session_file)
        if not resolved_file.exists():
            raise click.ClickException(f"File not found: {session_file}")
        derived_name = resolved_file.stem

    auto_open = output is None and not gist and not output_auto
    if output_auto:
        destination = output_auto_dir(output, derived_name)
    else:
        destination = output or default_output_dir(resolved_file)
        if output and output.suffix:
            destination = output.parent / output.stem

    result = generate_html(resolved_file, destination)
    index_path = result["output_dir"] / "index.html"
    click.echo(f"Transcript: {index_path}")
    if include_json:
        json_dest = copy_source_json(resolved_file, result["output_dir"])
        click.echo(f"JSON: {json_dest} ({json_dest.stat().st_size / 1024:.1f} KB)")
    if gist:
        inject_gist_preview_js(result["output_dir"])
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(result["output_dir"])
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")
    if should_open or auto_open:
        webbrowser.open(index_path.as_uri())


@cli.command()
@click.option("--source", type=click.Path(path_type=Path), default=Path.home() / ".codex" / "sessions")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("./codex-archive"))
@click.option("--include-agents", is_flag=True, help="Include agent session files.")
@click.option("--dry-run", is_flag=True, help="Show what would be converted without creating files.")
@click.option("--open", "should_open", is_flag=True, help="Open the generated archive in your default browser.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress all output except errors.")
def all(
    source: Path,
    output: Path,
    include_agents: bool,
    dry_run: bool,
    should_open: bool,
    quiet: bool,
) -> None:
    result = generate_batch_html(source, output, include_agents=include_agents, dry_run=dry_run)
    if dry_run:
        if not quiet:
            click.echo(
                f"Would convert {result['total_sessions']} sessions across {result['total_projects']} workspaces into {output}"
            )
            for project in result["projects"]:
                click.echo(f"{project['name']}: {len(project['sessions'])} sessions")
        return

    index_path = result["output_dir"] / "index.html"
    if not quiet:
        click.echo(
            f"Archived {result['total_sessions']} sessions across {result['total_projects']} workspaces to {index_path}"
        )
        if result["failed_sessions"]:
            click.echo(f"Skipped {len(result['failed_sessions'])} failed sessions:")
            for failed in result["failed_sessions"]:
                click.echo(f"- {failed['project']}/{failed['session']}: {failed['error']}")
    if should_open:
        webbrowser.open(index_path.resolve().as_uri())


def main() -> None:
    cli()


CSS = """
:root {
  --bg-color: #f5f5f5;
  --card-bg: #ffffff;
  --user-bg: #e3f2fd;
  --user-border: #1976d2;
  --assistant-bg: #f5f5f5;
  --assistant-border: #9e9e9e;
  --thinking-bg: #fff8e1;
  --thinking-border: #ffc107;
  --thinking-text: #666;
  --tool-bg: #f3e5f5;
  --tool-border: #9c27b0;
  --tool-result-bg: #e8f5e9;
  --tool-error-bg: #ffebee;
  --commentary-bg: #e8f1ff;
  --commentary-border: #5c6bc0;
  --system-bg: #eceff1;
  --system-border: #78909c;
  --text-color: #212121;
  --text-muted: #757575;
  --code-bg: #263238;
  --code-text: #aed581;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg-color);
  color: var(--text-color);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6;
}
.container {
  max-width: 960px;
  margin: 0 auto;
  padding: 28px 18px 56px;
}
.hero {
  background: var(--card-bg);
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  margin-bottom: 22px;
}
.hero h1, h1 {
  margin: 0 0 10px;
  line-height: 1.1;
}
.hero p {
  margin: 0;
  color: var(--text-muted);
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.header-row h1 {
  margin-bottom: 8px;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px;
  margin-top: 16px;
}
.meta-card {
  padding: 12px 14px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.02);
  border: 1px solid rgba(0, 0, 0, 0.08);
}
.meta-label {
  display: block;
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-bottom: 4px;
}
.summary {
  color: var(--text-muted);
  margin-bottom: 20px;
}
.turn-shell,
.index-item,
.message {
  border-radius: 12px;
  overflow: hidden;
  margin-bottom: 16px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.turn-shell {
  background: var(--card-bg);
}
.turn-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
  padding: 16px 18px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
  background: rgba(0,0,0,0.03);
}
.turn-title {
  font-size: 1.05rem;
  font-weight: 600;
}
.turn-status {
  color: var(--text-muted);
  font-size: 0.9rem;
}
.turn-body {
  padding: 14px;
}
.message.user { background: var(--user-bg); border-left: 4px solid var(--user-border); }
.message.assistant { background: var(--card-bg); border-left: 4px solid var(--assistant-border); }
.message.commentary { background: var(--commentary-bg); border-left: 4px solid var(--commentary-border); }
.message.system { background: var(--system-bg); border-left: 4px solid var(--system-border); }
.message.thinking { background: var(--thinking-bg); border-left: 4px solid var(--thinking-border); }
.message.tool { background: var(--tool-bg); border-left: 4px solid var(--tool-border); }
.message.tool-reply { background: #fff8e1; border-left: 4px solid #ff9800; }
.message-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  font-size: 0.82rem;
  background: rgba(0, 0, 0, 0.06);
}
.role-label {
  text-transform: uppercase;
  letter-spacing: 0.07em;
  font-weight: 600;
}
.message-content {
  padding: 14px;
}
.message-content p:first-child { margin-top: 0; }
.message-content p:last-child { margin-bottom: 0; }
.tool-use, .tool-result, .thinking, .meta-block, .user-content, .assistant-text {
  border-radius: 14px;
}
.tool-use {
  background: var(--tool-bg);
  border: 1px solid var(--tool-border);
  padding: 12px;
}
.tool-result {
  background: var(--tool-result-bg);
  padding: 12px;
}
.thinking {
  background: var(--thinking-bg);
  border: 1px solid var(--thinking-border);
  color: var(--thinking-text);
  padding: 12px;
}
.meta-block {
  background: rgba(0,0,0,0.03);
  padding: 12px;
}
.tool-header {
  font-weight: 700;
  margin-bottom: 8px;
}
pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  font-size: 0.84rem;
  background: var(--code-bg);
  color: var(--code-text);
  padding: 12px;
  border-radius: 6px;
}
code {
  font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  background: rgba(0,0,0,0.08);
  padding: 2px 6px;
  border-radius: 4px;
}
.pagination {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 18px 0 22px;
}
.pagination a, .pagination span {
  text-decoration: none;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--user-border);
  background: var(--card-bg);
  color: var(--user-border);
}
.pagination .current, .pagination .index-link {
  background: var(--user-border);
  color: white;
  border-color: var(--user-border);
}
.index-item {
  background: var(--user-bg);
  border-left: 4px solid var(--user-border);
}
.index-item a {
  color: inherit;
  text-decoration: none;
  display: block;
}
.index-item-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(0,0,0,0.08);
  background: rgba(0,0,0,0.03);
}
.index-item-content,
.index-item-stats {
  padding: 14px 16px;
}
.index-item-stats {
  color: var(--text-muted);
  font-size: 0.92rem;
  border-top: 1px solid rgba(0,0,0,0.08);
}
.index-item-stats > span {
  display: inline-block;
}
.index-item-long-text {
  margin-top: 10px;
  padding: 12px;
  background: var(--card-bg);
  border-radius: 10px;
  border-left: 3px solid var(--assistant-border);
}
.index-item-long-text-content {
  color: var(--text-color);
}
.index-item-long-text .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--card-bg));
}
.index-commit {
  margin-bottom: 12px;
  padding: 10px 16px;
  background: #fff3e0;
  border-left: 4px solid #ff9800;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
.index-commit a {
  display: block;
  text-decoration: none;
  color: inherit;
}
.index-commit a:hover {
  background: rgba(255, 152, 0, 0.1);
  margin: -10px -16px;
  padding: 10px 16px;
  border-radius: 10px;
}
.index-commit-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  font-size: 0.85rem;
  margin-bottom: 4px;
}
.index-commit-hash {
  font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  color: #e65100;
  font-weight: 600;
}
.index-commit-msg {
  color: #5d4037;
}
.index-commit time {
  color: var(--text-muted);
}
.timestamp-link {
  color: inherit;
  text-decoration: none;
}
.timestamp-link:hover {
  text-decoration: underline;
}
.truncatable { position: relative; }
.truncatable.truncated .truncatable-content {
  max-height: 220px;
  overflow: hidden;
}
.truncatable.truncated::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 38px;
  height: 72px;
  pointer-events: none;
  background: linear-gradient(to bottom, transparent, rgba(255,255,255,0.96));
}
.message.user .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--user-bg));
}
.message.assistant .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--card-bg));
}
.message.commentary .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--commentary-bg));
}
.message.system .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--system-bg));
}
.message.thinking .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--thinking-bg));
}
.message.tool .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, var(--tool-bg));
}
.message.tool-reply .truncatable.truncated::after {
  background: linear-gradient(to bottom, transparent, #fff8e1);
}
.expand-btn {
  display: none;
  width: 100%;
  margin-top: 8px;
  border: 1px solid rgba(0,0,0,0.12);
  background: rgba(255,255,255,0.72);
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 0.85rem;
}
.truncatable.truncated .expand-btn,
.truncatable.expanded .expand-btn {
  display: block;
}
#search-box {
  display: none;
  align-items: center;
  gap: 8px;
}
#search-box input,
.search-modal-header input {
  padding: 8px 12px;
  border: 1px solid rgba(0,0,0,0.14);
  border-radius: 8px;
  font-size: 16px;
  background: var(--card-bg);
}
#search-box input {
  min-width: 180px;
}
#search-box button,
#modal-search-btn,
#modal-close-btn {
  background: var(--user-border);
  color: white;
  border: none;
  border-radius: 8px;
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
#search-box button:hover,
#modal-search-btn:hover {
  background: #1565c0;
}
#modal-close-btn {
  background: var(--text-muted);
  margin-left: 8px;
}
#modal-close-btn:hover {
  background: #616161;
}
#search-modal[open] {
  border: none;
  border-radius: 14px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.2);
  padding: 0;
  width: min(92vw, 900px);
  height: min(82vh, 720px);
  max-height: 82vh;
  display: flex;
  flex-direction: column;
}
#search-modal::backdrop {
  background: rgba(0,0,0,0.5);
}
.search-modal-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  border-bottom: 1px solid rgba(0,0,0,0.08);
  background: var(--bg-color);
  border-radius: 14px 14px 0 0;
}
.search-modal-header input {
  flex: 1;
}
#search-status {
  padding: 8px 16px;
  font-size: 0.85rem;
  color: var(--text-muted);
  border-bottom: 1px solid rgba(0,0,0,0.06);
}
#search-results {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.search-result {
  margin-bottom: 16px;
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.search-result a {
  display: block;
  text-decoration: none;
  color: inherit;
}
.search-result a:hover {
  background: rgba(25, 118, 210, 0.05);
}
.search-result-page {
  padding: 6px 12px;
  background: rgba(0,0,0,0.03);
  font-size: 0.8rem;
  color: var(--text-muted);
  border-bottom: 1px solid rgba(0,0,0,0.06);
}
.search-result-content {
  padding: 12px;
}
.search-result mark {
  background: #fff59d;
  padding: 1px 2px;
  border-radius: 2px;
}
@media (max-width: 640px) {
  .container { padding: 16px 10px 32px; }
  .hero { padding: 18px; border-radius: 12px; }
  .turn-head { display: block; }
  .header-row { align-items: flex-start; }
  #search-box input { min-width: 120px; }
  #search-modal[open] { width: 95vw; height: 90vh; max-height: 90vh; }
}
"""


JS = """
document.querySelectorAll('time[data-timestamp]').forEach(function(el) {
  const timestamp = el.getAttribute('data-timestamp');
  const date = new Date(timestamp);
  if (String(date) === 'Invalid Date') return;
  el.textContent = date.toLocaleString();
});
document.querySelectorAll('.truncatable').forEach(function(wrapper) {
  const content = wrapper.querySelector('.truncatable-content');
  const btn = wrapper.querySelector('.expand-btn');
  if (!content || !btn) return;
  if (content.scrollHeight > 250) {
    wrapper.classList.add('truncated');
    btn.addEventListener('click', function() {
      if (wrapper.classList.contains('truncated')) {
        wrapper.classList.remove('truncated');
        wrapper.classList.add('expanded');
        btn.textContent = 'Show less';
      } else {
        wrapper.classList.remove('expanded');
        wrapper.classList.add('truncated');
        btn.textContent = 'Show more';
      }
    });
  }
});
"""
