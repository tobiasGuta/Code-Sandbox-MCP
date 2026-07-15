from __future__ import annotations

import pytest

from code_sandbox_mcp.audit import AuditLogger
from code_sandbox_mcp.config import SandboxConfig
from code_sandbox_mcp.server import set_manager_for_tests
from code_sandbox_mcp.session import SessionManager

from .fakes import FakeBackend


@pytest.fixture
def manager(tmp_path):
    config = SandboxConfig(
        audit_enabled=True,
        audit_path=tmp_path / "audit.jsonl",
        max_session_lifetime_seconds=60,
        idle_timeout_seconds=30,
    )
    backend = FakeBackend(config)
    value = SessionManager(config, backend, AuditLogger(True, config.audit_path), start_reaper=False)
    set_manager_for_tests(value)
    yield value
    set_manager_for_tests(None)
