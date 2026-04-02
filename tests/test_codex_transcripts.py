import json
import re
from pathlib import Path

import pytest

import codex_transcripts
from codex_transcripts import generate_batch_html, generate_html, parse_session_file


def write_jsonl_session(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def sample_session_entries(workspace_name: str = "demo-workspace") -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-04-01T10:00:00Z",
            "payload": {
                "id": "session-123",
                "cwd": f"/tmp/{workspace_name}",
                "originator": "Codex",
                "model_provider": "OpenAI",
                "cli_version": "0.1.0",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-04-01T10:00:01Z",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Export this transcript"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:03Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Working on it."}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-04-01T10:00:04Z",
            "payload": {"type": "task_complete"},
        },
    ]


def index_parity_session_path() -> Path:
    return Path(__file__).parent / "fixtures" / "index_parity_session.jsonl"


def test_parse_session_file_supports_json_array(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps(sample_session_entries(), ensure_ascii=False),
        encoding="utf-8",
    )

    meta, turns = parse_session_file(session_file)

    assert meta["id"] == "session-123"
    assert len(turns) == 1
    assert turns[0].prompt_preview == "Export this transcript"
    assert turns[0].status == "completed"


def test_parse_session_file_supports_json_object_with_loglines(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps({"loglines": sample_session_entries()}, ensure_ascii=False),
        encoding="utf-8",
    )

    meta, turns = parse_session_file(session_file)

    assert meta["id"] == "session-123"
    assert len(turns) == 1
    assert turns[0].prompt_preview == "Export this transcript"


def test_generate_html_escapes_embedded_html_in_json_blocks(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    entries = sample_session_entries()
    entries.insert(
        3,
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02.500000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "custom_payload",
                        "text": "<script>alert('xss')</script>",
                    }
                ],
            },
        },
    )
    write_jsonl_session(session_file, entries)

    output_dir = tmp_path / "output"
    generate_html(session_file, output_dir)
    html = (output_dir / "page-001.html").read_text(encoding="utf-8")

    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html


def test_generate_html_escapes_embedded_html_in_custom_tool_output(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    entries = sample_session_entries()
    entries.insert(
        3,
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02.500000Z",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call-123",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Add File: /tmp/x.txt\n+hello\n*** End Patch\n",
            },
        },
    )
    entries.insert(
        4,
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02.600000Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-123",
                "output": json.dumps(
                    {
                        "output": "<b>patched</b>",
                        "metadata": {"exit_code": 0},
                    },
                    ensure_ascii=False,
                ),
            },
        },
    )
    write_jsonl_session(session_file, entries)

    output_dir = tmp_path / "output"
    generate_html(session_file, output_dir)
    html = (output_dir / "page-001.html").read_text(encoding="utf-8")

    assert "<b>patched</b>" not in html
    assert "&lt;b&gt;patched&lt;/b&gt;" in html


def test_generate_batch_html_skips_failed_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_dir = tmp_path / "sessions"
    good_session = source_dir / "workspace-a" / "good.jsonl"
    broken_session = source_dir / "workspace-a" / "broken.jsonl"

    write_jsonl_session(good_session, sample_session_entries("workspace-a"))
    write_jsonl_session(broken_session, sample_session_entries("workspace-a"))

    original_generate_html = codex_transcripts.generate_html

    def flaky_generate_html(input_path: Path, output_dir: Path) -> dict:
        if input_path.stem == "broken":
            raise RuntimeError("simulated render failure")
        return original_generate_html(input_path, output_dir)

    monkeypatch.setattr(codex_transcripts, "generate_html", flaky_generate_html)

    output_dir = tmp_path / "archive"
    result = generate_batch_html(source_dir, output_dir)

    assert result["total_projects"] == 1
    assert result["total_sessions"] == 1
    assert len(result["failed_sessions"]) == 1
    assert result["failed_sessions"][0]["session"] == "broken"
    assert (output_dir / "workspace-a" / "good" / "index.html").exists()
    assert (output_dir / "workspace-a" / "index.html").exists()


def test_generate_batch_html_dry_run_reports_counts(tmp_path: Path) -> None:
    source_dir = tmp_path / "sessions"
    write_jsonl_session(source_dir / "workspace-a" / "good.jsonl", sample_session_entries())

    result = generate_batch_html(source_dir, tmp_path / "archive", dry_run=True)

    assert result["total_projects"] == 1
    assert result["total_sessions"] == 1


def test_real_excerpt_skips_mirrored_events_and_keeps_custom_tool_output(tmp_path: Path) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "real_session_excerpt.jsonl"
    output_dir = tmp_path / "output"

    result = generate_html(fixture_path, output_dir)
    turns = result["turns"]
    page_html = (output_dir / "page-001.html").read_text(encoding="utf-8")

    assert len(turns) == 1
    assert turns[0].tool_calls == 1
    assert page_html.count("我先看一下目录结构。") == 1
    assert "Turn Context" not in page_html
    assert "Success. Updated the following files:" in page_html


def test_real_excerpt_generates_expected_index_and_page_content(tmp_path: Path) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "real_session_excerpt.jsonl"
    output_dir = tmp_path / "output"

    generate_html(fixture_path, output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    page_html = (output_dir / "page-001.html").read_text(encoding="utf-8")

    assert "real-excerpt-1" in index_html
    assert "Codex Desktop transcript" in index_html
    assert "1 turns" in index_html
    assert "4 entries" in index_html
    assert "1 tool calls" in index_html
    assert "请帮我整理这个目录。" in index_html
    assert "apply_patch" in page_html
    assert "*** Begin Patch" in page_html
    assert "Tool Result" in page_html
    assert "Task Complete" not in page_html


def test_index_page_includes_search_ui_and_progressive_enhancement_hooks(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"

    generate_html(index_parity_session_path(), output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")

    assert 'id="search-box"' in index_html
    assert 'id="search-input"' in index_html
    assert 'id="search-modal"' in index_html
    assert 'id="modal-search-input"' in index_html
    assert 'id="search-results"' in index_html
    assert "searchBox.style.display = 'flex';" in index_html
    assert "window.location.protocol === 'file:'" in index_html


def test_index_page_shows_long_assistant_preview_when_output_is_long(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"

    generate_html(index_parity_session_path(), output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")

    assert "index-item-long-text" in index_html
    assert "Show more" in index_html
    assert "This assistant response is intentionally long" in index_html


def test_index_page_includes_commit_timeline_entry_for_commit_tool_output(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"

    generate_html(index_parity_session_path(), output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")

    assert "index-commit" in index_html
    assert "abc1234" in index_html
    assert "Add release notes" in index_html


def test_index_page_embeds_search_dataset_for_file_protocol_usage(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"

    generate_html(index_parity_session_path(), output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")

    assert "window.__TRANSCRIPT_SEARCH_DATA__ =" in index_html
    assert "page-001.html" in index_html
    assert "This assistant response is intentionally long" in index_html


def test_index_page_escapes_script_terminators_in_embedded_search_data(
    tmp_path: Path,
) -> None:
    session_file = tmp_path / "session.jsonl"
    entries = sample_session_entries()
    entries.insert(
        3,
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02.500000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Contains </script><script>alert('x')</script> in transcript text.",
                    }
                ],
            },
        },
    )
    write_jsonl_session(session_file, entries)

    output_dir = tmp_path / "output"
    generate_html(session_file, output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")

    assert "window.__TRANSCRIPT_SEARCH_DATA__ =" in index_html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert('x')\\u003c/script\\u003e" in index_html
    assert "</script><script>alert('x')</script>" not in index_html


def test_commit_timeline_message_is_cleaned_from_tool_output_wrapper(
    tmp_path: Path,
) -> None:
    session_file = tmp_path / "session.jsonl"
    entries = sample_session_entries()
    entries.insert(
        3,
        {
            "type": "response_item",
            "timestamp": "2026-04-01T10:00:02.500000Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-123",
                "output": json.dumps(
                    {
                        "output": '[main abc1234] Add release notes\\n 1 file changed, 42 insertions(+)\\n',
                        "metadata": {"exit_code": 0},
                    },
                    ensure_ascii=False,
                ),
            },
        },
    )
    write_jsonl_session(session_file, entries)

    output_dir = tmp_path / "output"
    generate_html(session_file, output_dir)
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<div class="index-commit-msg">([^<]+)</div>', index_html)

    assert match is not None
    assert match.group(1) == "Add release notes"
    assert "&#34;}}" not in index_html
