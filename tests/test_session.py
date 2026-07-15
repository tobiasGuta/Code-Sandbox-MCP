from __future__ import annotations

import time

import pytest

from code_sandbox_mcp.audit import AuditLogger
from code_sandbox_mcp.config import SandboxConfig
from code_sandbox_mcp.errors import ErrorCode, SandboxError
from code_sandbox_mcp.session import SessionManager

from .fakes import FakeBackend, FakeContainer


def create_id(manager):
    result = manager.create()
    assert result["profile"] == "javascript-offline"
    assert "sha256" not in result["session_id"]
    return result["session_id"]


def test_functional_workspace_flow(manager):
    session_id = create_id(manager)
    result = manager.write_files(session_id, [
        {"path": "index.js", "content": "console.log('hello')"},
        {"path": "lib/parser.js", "content": "export const parse = x => x.trim()"},
    ], overwrite=False)
    assert [item["status"] for item in result["files"]] == ["written", "written"]

    listed = manager.list_files(session_id, ".", True, 5)
    assert {item["path"] for item in listed["files"]} == {"index.js", "lib", "lib/parser.js"}
    read = manager.read_file(session_id, "index.js", 8)
    assert read == {"ok": True, "path": "index.js", "content": "console.", "size": 20, "truncated": True}

    exists = manager.write_files(session_id, [{"path": "index.js", "content": "new"}], overwrite=False)
    assert exists["files"][0]["status"] == "exists"
    overwritten = manager.write_files(
        session_id,
        [{"path": "index.js", "content": "console.error('problem')"}],
        overwrite=True,
    )
    assert overwritten["files"][0]["status"] == "overwritten"

    execution = manager.run_javascript(session_id, "index.js", ["example"], 20)
    assert execution["exit_code"] == 0
    assert execution["stderr"] == "problem\n"
    assert manager.backend.last_run == (
        "node", ["--disable-proto=throw", "/workspace/index.js", "example"], 20,
    )

    deleted = manager.delete_files(session_id, ["lib/parser.js", "missing.js"])
    assert deleted["deleted"] == ["lib/parser.js"]
    assert deleted["missing"] == ["missing.js"]
    assert manager.destroy(session_id) == {"ok": True, "destroyed": True}
    assert manager.destroy(session_id) == {"ok": True, "destroyed": False}
    assert manager.backend.created[0].removed is True


def test_limits_and_binary_reads(manager):
    session_id = create_id(manager)
    with pytest.raises(SandboxError) as caught:
        manager.write_files(session_id, [{"path": "large.js", "content": "x" * (2 * 1024 * 1024 + 1)}], False)
    assert caught.value.code == ErrorCode.FILE_TOO_LARGE
    manager.backend.created[0].files["binary.dat"] = b"a\x00b"
    with pytest.raises(SandboxError) as caught:
        manager.read_file(session_id, "binary.dat", 10)
    assert caught.value.code == ErrorCode.BINARY_FILE


def test_expiration_destroys_container(manager):
    session_id = create_id(manager)
    session = manager._sessions[session_id]
    session.expires_monotonic = time.monotonic() - 1
    with pytest.raises(SandboxError) as caught:
        with manager.lease(session_id):
            pass
    assert caught.value.code == ErrorCode.SESSION_EXPIRED
    assert manager.backend.created[0].removed is True


def test_close_removes_every_container(manager):
    create_id(manager)
    create_id(manager)
    manager.close()
    assert all(container.removed for container in manager.backend.created)


def test_abort_removes_in_flight_session_without_waiting_for_normal_destroy(manager):
    session_id = create_id(manager)
    manager.abort(session_id)
    assert session_id not in manager._sessions
    assert manager.backend.created[0].removed is True


def test_failed_removal_keeps_session_retryable(manager):
    session_id = create_id(manager)
    container = manager.backend.created[0]
    container.destroy_failures = 1

    with pytest.raises(SandboxError) as caught:
        manager.destroy(session_id)
    assert caught.value.code == ErrorCode.CONTAINER_REMOVAL_FAILED
    assert manager._sessions[session_id].destroying is True
    assert session_id in manager._pending_removals
    assert container.removed is False

    assert manager.destroy(session_id) == {"ok": True, "destroyed": True}
    assert session_id not in manager._sessions
    assert session_id not in manager._pending_removals
    assert container.removed is True


def test_reaper_retries_failed_removal(manager):
    session_id = create_id(manager)
    container = manager.backend.created[0]
    container.destroy_failures = 1
    with pytest.raises(SandboxError):
        manager.destroy(session_id)

    manager._retry_pending_removals()
    assert session_id not in manager._sessions
    assert container.removed is True


def test_startup_orphan_failure_is_queued_and_retried(tmp_path):
    config = SandboxConfig(audit_enabled=False, audit_path=tmp_path / "audit")
    backend = FakeBackend(config)
    orphan = FakeContainer(destroy_failures=1)
    backend.startup_orphans.append(orphan)

    manager = SessionManager(config, backend, AuditLogger(False, config.audit_path), start_reaper=False)
    try:
        assert len(manager._pending_removals) == 1
        assert orphan.removed is False
        manager._retry_pending_removals()
        assert manager._pending_removals == {}
        assert orphan.removed is True
    finally:
        manager.close()


def test_concurrency_limit(manager):
    for _ in range(manager.config.max_concurrent_sessions):
        create_id(manager)
    with pytest.raises(SandboxError) as caught:
        manager.create()
    assert caught.value.code == ErrorCode.WORKSPACE_LIMIT_EXCEEDED
