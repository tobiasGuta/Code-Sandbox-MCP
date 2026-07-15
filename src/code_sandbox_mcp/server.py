from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError

from .audit import AuditLogger
from .config import SandboxConfig
from .docker_backend import DockerBackend
from .errors import ErrorCode, SandboxError
from .models import (
    DeleteFilesRequest,
    FileWrite,
    ListFilesRequest,
    ReadFileRequest,
    RunJavascriptRequest,
    SessionRequest,
    WriteFilesRequest,
)
from .session import SessionManager

mcp = FastMCP(
    name="code-sandbox",
    instructions=(
        "Creates disposable, offline JavaScript workspaces. Use only the fixed file and "
        "JavaScript operations; always destroy a sandbox when work is complete."
    ),
)

_manager: SessionManager | None = None
_manager_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def get_manager() -> SessionManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            config = SandboxConfig.from_env()
            audit = AuditLogger(config.audit_enabled, config.audit_path)
            _manager = SessionManager(config, DockerBackend(config), audit)
        return _manager


def set_manager_for_tests(manager: SessionManager | None) -> None:
    global _manager
    with _manager_lock:
        if _manager is not None and _manager is not manager:
            _manager.close()
        _manager = manager


def _safe_validation_error(error: ValidationError) -> dict[str, object]:
    first = error.errors(include_input=False, include_context=False)[0]
    location = ".".join(str(item) for item in first.get("loc", ())) or "request"
    return {
        "ok": False,
        "error": {
            "code": ErrorCode.INVALID_REQUEST.value,
            "message": f"invalid {location}: {first.get('msg', 'validation failed')}",
        },
    }


def _safe_audit(
    manager: SessionManager,
    tool: str,
    session_id: str | None,
    result: str,
    duration_ms: int,
    **fields: Any,
) -> None:
    try:
        written = manager.audit.log(tool, session_id, result, duration_ms, **fields)
        if written is False:
            _logger.warning("sandbox audit event could not be written")
    except Exception:
        # Execution and cleanup results must not be lost because an optional
        # local audit sink is unavailable or a custom logger misbehaves.
        _logger.warning("sandbox audit event could not be written")


def _call(
    tool: str,
    session_id: str | None,
    operation: Callable[[SessionManager], dict[str, Any]],
    **audit_fields: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    manager: SessionManager | None = None
    try:
        manager = get_manager()
        result = operation(manager)
        if isinstance(result, dict):
            audit_fields.setdefault("exit_code", result.get("exit_code"))
            audit_fields.setdefault("timed_out", result.get("timed_out"))
            if isinstance(result.get("stdout"), str):
                audit_fields.setdefault("stdout_bytes", len(result["stdout"].encode("utf-8")))
            if isinstance(result.get("stderr"), str):
                audit_fields.setdefault("stderr_bytes", len(result["stderr"].encode("utf-8")))
            if tool == "destroy_sandbox":
                audit_fields.setdefault("cleanup_result", "removed" if result.get("destroyed") else "already_absent")
            if session_id is None and isinstance(result.get("session_id"), str):
                session_id = result["session_id"]
        _safe_audit(manager, tool, session_id, "ok", round((time.monotonic() - started) * 1000), **audit_fields)
        return result
    except SandboxError as error:
        if manager is not None:
            _safe_audit(
                manager,
                tool,
                session_id,
                error.code.value,
                round((time.monotonic() - started) * 1000),
                **audit_fields,
            )
        return error.as_dict()
    except Exception:
        if manager is not None:
            _safe_audit(
                manager,
                tool,
                session_id,
                ErrorCode.INTERNAL_ERROR.value,
                round((time.monotonic() - started) * 1000),
                **audit_fields,
            )
        return SandboxError(ErrorCode.INTERNAL_ERROR, "internal sandbox error").as_dict()


@mcp.tool()
def create_sandbox() -> dict[str, Any]:
    """Create a disposable offline JavaScript sandbox using the fixed server profile."""
    return _call("create_sandbox", None, lambda manager: manager.create())


@mcp.tool()
def write_files(
    session_id: Annotated[str, Field(min_length=32, max_length=128)],
    files: Annotated[list[FileWrite], Field(min_length=1, max_length=100)],
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write UTF-8 text files beneath /workspace using an in-memory archive."""
    try:
        request = WriteFilesRequest(session_id=session_id, files=files, overwrite=overwrite)
    except ValidationError as error:
        return _safe_validation_error(error)
    except SandboxError as error:
        return error.as_dict()
    submitted_bytes = sum(len(item.content.encode("utf-8")) for item in request.files)
    return _call(
        "write_files",
        request.session_id,
        lambda manager: manager.write_files(
            request.session_id,
            [item.model_dump() for item in request.files],
            request.overwrite,
        ),
        file_count=len(request.files),
        submitted_bytes=submitted_bytes,
    )


@mcp.tool()
def list_files(
    session_id: Annotated[str, Field(min_length=32, max_length=128)],
    path: str = ".",
    recursive: bool = True,
    max_depth: Annotated[int, Field(ge=0, le=10)] = 5,
) -> dict[str, Any]:
    """List regular files and directories beneath /workspace without following links."""
    try:
        request = ListFilesRequest(session_id=session_id, path=path, recursive=recursive, max_depth=max_depth)
    except ValidationError as error:
        return _safe_validation_error(error)
    except SandboxError as error:
        return error.as_dict()
    return _call(
        "list_files", request.session_id,
        lambda manager: manager.list_files(request.session_id, request.path, request.recursive, request.max_depth),
    )


@mcp.tool()
def read_file(
    session_id: Annotated[str, Field(min_length=32, max_length=128)],
    path: str,
    max_bytes: Annotated[int, Field(ge=1, le=2 * 1024 * 1024)] = 65536,
) -> dict[str, Any]:
    """Read a bounded UTF-8 text file beneath /workspace."""
    try:
        request = ReadFileRequest(session_id=session_id, path=path, max_bytes=max_bytes)
    except ValidationError as error:
        return _safe_validation_error(error)
    except SandboxError as error:
        return error.as_dict()
    return _call(
        "read_file", request.session_id,
        lambda manager: manager.read_file(request.session_id, request.path, request.max_bytes),
    )


@mcp.tool()
def delete_files(
    session_id: Annotated[str, Field(min_length=32, max_length=128)],
    paths: Annotated[list[str], Field(min_length=1, max_length=100)],
) -> dict[str, Any]:
    """Delete selected regular files; deleting /workspace or directories is forbidden."""
    try:
        request = DeleteFilesRequest(session_id=session_id, paths=paths)
    except ValidationError as error:
        return _safe_validation_error(error)
    except SandboxError as error:
        return error.as_dict()
    return _call(
        "delete_files", request.session_id,
        lambda manager: manager.delete_files(request.session_id, request.paths),
        file_count=len(request.paths),
    )


@mcp.tool()
async def run_javascript(
    session_id: Annotated[str, Field(min_length=32, max_length=128)],
    entrypoint: str,
    arguments: Annotated[list[str], Field(max_length=32)] | None = None,
    timeout_seconds: Annotated[int | None, Field(ge=1, le=120)] = None,
) -> dict[str, Any]:
    """Run an existing JavaScript file with fixed Node arguments and no shell."""
    try:
        request = RunJavascriptRequest(
            session_id=session_id,
            entrypoint=entrypoint,
            arguments=arguments or [],
            timeout_seconds=timeout_seconds,
        )
    except ValidationError as error:
        return _safe_validation_error(error)
    except SandboxError as error:
        return error.as_dict()

    def run(manager: SessionManager) -> dict[str, Any]:
        timeout = request.timeout_seconds or manager.config.default_command_timeout_seconds
        if timeout > manager.config.max_command_timeout_seconds:
            raise SandboxError(ErrorCode.INVALID_REQUEST, "timeout exceeds the server maximum")
        return manager.run_javascript(request.session_id, request.entrypoint, request.arguments, timeout)

    try:
        return await asyncio.to_thread(_call, "run_javascript", request.session_id, run)
    except asyncio.CancelledError:
        manager = get_manager()
        cleanup_result = "removed"
        try:
            await asyncio.shield(asyncio.to_thread(manager.abort, request.session_id))
        except SandboxError:
            cleanup_result = "removal_failed"
        _safe_audit(
            manager,
            "run_javascript",
            request.session_id,
            "REQUEST_CANCELLED",
            0,
            cleanup_result=cleanup_result,
        )
        raise


@mcp.tool()
def destroy_sandbox(session_id: Annotated[str, Field(min_length=32, max_length=128)]) -> dict[str, Any]:
    """Force-remove a sandbox. Repeated calls are safe and idempotent."""
    try:
        request = SessionRequest(session_id=session_id)
    except ValidationError as error:
        return _safe_validation_error(error)
    return _call("destroy_sandbox", request.session_id, lambda manager: manager.destroy(request.session_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardened offline JavaScript sandbox MCP server")
    parser.parse_args()
    try:
        mcp.run(transport="stdio")
    finally:
        with _manager_lock:
            manager = _manager
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    main()
