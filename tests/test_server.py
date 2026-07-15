from __future__ import annotations

import asyncio
import json

from code_sandbox_mcp.models import FileWrite
from code_sandbox_mcp.server import (
    create_sandbox,
    destroy_sandbox,
    list_files,
    read_file,
    run_javascript,
    write_files,
)


def test_mcp_surface_is_minimal_and_bounded():
    from code_sandbox_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "create_sandbox",
        "write_files",
        "list_files",
        "read_file",
        "delete_files",
        "run_javascript",
        "destroy_sandbox",
    }
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert schemas["create_sandbox"]["properties"] == {}
    assert schemas["write_files"]["properties"]["files"]["maxItems"] == 100
    assert schemas["read_file"]["properties"]["max_bytes"]["maximum"] == 2 * 1024 * 1024
    assert schemas["run_javascript"]["properties"]["arguments"]["anyOf"][0]["maxItems"] == 32


def test_tools_return_structured_results_and_audit(manager):
    created = create_sandbox()
    session_id = created["session_id"]
    assert write_files(session_id, [FileWrite(path="index.js", content="console.log('hello')")])["ok"]
    assert list_files(session_id)["files"][0]["path"] == "index.js"
    assert read_file(session_id, "index.js")["content"].startswith("console")
    assert asyncio.run(run_javascript(session_id, "index.js"))["stdout"] == "hello\n"
    assert destroy_sandbox(session_id)["destroyed"] is True

    records = [json.loads(line) for line in manager.config.audit_path.read_text().splitlines()]
    assert [record["tool"] for record in records] == [
        "create_sandbox", "write_files", "list_files", "read_file", "run_javascript", "destroy_sandbox",
    ]
    assert all("source" not in record for record in records)
    assert records[0]["session_hash"] != session_id
    assert records[-1]["cleanup_result"] == "removed"


def test_tool_validation_does_not_echo_sensitive_input(manager):
    sensitive_value = "SECRET_" + "SHOULD_NOT_BE_ECHOED"
    result = write_files("x", [FileWrite(path="index.js", content=sensitive_value)])
    serialized = json.dumps(result)
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert sensitive_value not in serialized
    assert "../secret" not in serialized


def test_audit_sink_failure_does_not_hide_successful_create_or_destroy(manager, monkeypatch):
    def fail_audit(*args, **kwargs):
        del args, kwargs
        raise OSError("simulated unavailable audit sink")

    monkeypatch.setattr(manager.audit, "log", fail_audit)
    created = create_sandbox()
    assert created["profile"] == "javascript-offline"
    assert created["session_id"] in manager._sessions
    assert destroy_sandbox(created["session_id"]) == {"ok": True, "destroyed": True}


def test_audit_logger_returns_false_on_filesystem_error(tmp_path):
    from code_sandbox_mcp.audit import AuditLogger

    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    logger = AuditLogger(True, blocked_parent / "audit.jsonl")
    assert logger.log("create_sandbox", None, "ok", 1) is False
