from __future__ import annotations

import atexit
import base64
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from .audit import AuditLogger
from .config import SandboxConfig
from .docker_backend import DockerSession
from .errors import ErrorCode, SandboxError

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Session:
    session_id: str
    docker: DockerSession
    created_monotonic: float
    expires_monotonic: float
    expires_at: datetime
    last_used_monotonic: float
    destroying: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    removal_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class PendingRemoval:
    key: str
    docker: DockerSession
    session: Session | None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionManager:
    def __init__(
        self,
        config: SandboxConfig,
        backend: Any,
        audit: AuditLogger,
        *,
        start_reaper: bool = True,
    ) -> None:
        self.config = config
        self.backend = backend
        self.audit = audit
        self._sessions: dict[str, Session] = {}
        self._pending_removals: dict[str, PendingRemoval] = {}
        self._registry_lock = threading.RLock()
        self._creating = 0
        self._closed = False
        self._owner = secrets.token_hex(16)
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self._queue_startup_orphans()
        if start_reaper:
            self._reaper = threading.Thread(target=self._reaper_loop, name="sandbox-reaper", daemon=True)
            self._reaper.start()
        atexit.register(self.close)

    def create(self) -> dict[str, Any]:
        with self._registry_lock:
            if self._closed:
                raise SandboxError(ErrorCode.INTERNAL_ERROR, "sandbox manager is shutting down")
            if len(self._sessions) + self._creating >= self.config.max_concurrent_sessions:
                raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "maximum concurrent sessions reached")
            self._creating += 1
        session_id = secrets.token_urlsafe(32)
        owner_label = hashlib.sha256(f"{self._owner}:{session_id}".encode()).hexdigest()[:32]
        now_mono = time.monotonic()
        now_wall = datetime.now(UTC)
        expires_at = now_wall + timedelta(seconds=self.config.max_session_lifetime_seconds)
        try:
            docker_session = self.backend.create(owner_label, int(expires_at.timestamp()))
        finally:
            with self._registry_lock:
                self._creating -= 1
        session = Session(
            session_id=session_id,
            docker=docker_session,
            created_monotonic=now_mono,
            expires_monotonic=now_mono + self.config.max_session_lifetime_seconds,
            expires_at=expires_at,
            last_used_monotonic=now_mono,
        )
        with self._registry_lock:
            if self._closed:
                self.backend.destroy(docker_session)
                raise SandboxError(ErrorCode.INTERNAL_ERROR, "sandbox manager is shutting down")
            self._sessions[session_id] = session
        return {
            "session_id": session_id,
            "expires_at": expires_at.isoformat(),
            "profile": "javascript-offline",
        }

    def _expired(self, session: Session, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return (
            current >= session.expires_monotonic
            or current - session.last_used_monotonic >= self.config.idle_timeout_seconds
        )

    @contextmanager
    def lease(self, session_id: str) -> Iterator[Session]:
        with self._registry_lock:
            session = self._sessions.get(session_id)
            destroying = session.destroying if session is not None else False
        if session is None or destroying:
            raise SandboxError(ErrorCode.INVALID_SESSION, "session is invalid or has been destroyed")
        session.lock.acquire()
        try:
            with self._registry_lock:
                if self._sessions.get(session_id) is not session or session.destroying:
                    raise SandboxError(ErrorCode.INVALID_SESSION, "session is invalid or has been destroyed")
            if self._expired(session):
                self._destroy_locked(session)
                raise SandboxError(ErrorCode.SESSION_EXPIRED, "session has expired and was destroyed")
            session.last_used_monotonic = time.monotonic()
            yield session
            session.last_used_monotonic = time.monotonic()
        finally:
            session.lock.release()

    def _inventory(self, session: Session) -> list[dict[str, Any]]:
        response = self.backend.helper(
            session.docker,
            "inventory",
            {"path": ".", "limit": self.config.max_files + 1},
        )
        entries = response["entries"]
        if response.get("truncated") or len(entries) > self.config.max_files:
            raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "workspace contains too many entries")
        total_size = 0
        for entry in entries:
            if entry["type"] not in {"file", "directory"}:
                raise SandboxError(ErrorCode.UNSAFE_FILE_TYPE, "workspace contains a symlink or special file")
            if entry["type"] == "file" and entry.get("links") != 1:
                raise SandboxError(ErrorCode.UNSAFE_FILE_TYPE, "workspace contains a hard-linked file")
            if len(entry["path"].split("/")) > 20:
                raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "workspace path depth exceeded")
            if entry["type"] == "file":
                if entry["size"] > self.config.max_file_bytes:
                    raise SandboxError(ErrorCode.FILE_TOO_LARGE, "workspace contains an oversized file")
                total_size += entry["size"]
        if total_size > self.config.max_workspace_bytes:
            raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "workspace size limit exceeded")
        return entries

    def write_files(self, session_id: str, files: list[dict[str, str]], overwrite: bool) -> dict[str, Any]:
        with self.lease(session_id) as session:
            entries = self._inventory(session)
            existing = {entry["path"]: entry for entry in entries}
            statuses: list[dict[str, str]] = []
            selected: list[tuple[str, bytes]] = []
            final_files = {path: entry["size"] for path, entry in existing.items() if entry["type"] == "file"}
            for item in files:
                relative = item["path"]
                content = item["content"].encode("utf-8")
                if len(content) > self.config.max_file_bytes:
                    raise SandboxError(ErrorCode.FILE_TOO_LARGE, f"file exceeds {self.config.max_file_bytes} bytes")
                target = existing.get(relative)
                if target and target["type"] != "file":
                    raise SandboxError(ErrorCode.INVALID_PATH, "file path collides with a directory")
                if target and not overwrite:
                    statuses.append({"path": relative, "status": "exists"})
                    continue
                parent = PurePosixPath(relative).parent
                while parent.as_posix() != ".":
                    parent_path = parent.as_posix()
                    parent_entry = existing.get(parent_path)
                    if parent_entry and parent_entry["type"] != "directory":
                        raise SandboxError(ErrorCode.INVALID_PATH, "parent path is not a directory")
                    parent = parent.parent
                selected.append((relative, content))
                final_files[relative] = len(content)
                statuses.append({"path": relative, "status": "overwritten" if target else "written"})
            if len(final_files) > self.config.max_files:
                raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "workspace file-count limit exceeded")
            if sum(final_files.values()) > self.config.max_workspace_bytes:
                raise SandboxError(ErrorCode.WORKSPACE_LIMIT_EXCEEDED, "workspace size limit exceeded")
            if selected:
                try:
                    self.backend.put_files(session.docker, selected)
                    self._inventory(session)
                except Exception:
                    self._destroy_locked(session)
                    raise
            return {"ok": True, "files": statuses}

    def list_files(self, session_id: str, path: str, recursive: bool, max_depth: int) -> dict[str, Any]:
        with self.lease(session_id) as session:
            self._inventory(session)
            response = self.backend.helper(
                session.docker,
                "list",
                {"path": path, "recursive": recursive, "max_depth": max_depth, "limit": self.config.max_files + 1},
            )
            files = [{key: entry[key] for key in ("path", "type", "size")} for entry in response["entries"]]
            return {"ok": True, "files": files[: self.config.max_files], "truncated": bool(response["truncated"])}

    def read_file(self, session_id: str, path: str, max_bytes: int) -> dict[str, Any]:
        with self.lease(session_id) as session:
            self._inventory(session)
            response = self.backend.helper(session.docker, "read", {"path": path, "max_bytes": max_bytes})
            content_bytes = base64.b64decode(response["content_base64"], validate=True)
            if b"\x00" in content_bytes:
                raise SandboxError(ErrorCode.BINARY_FILE, "binary files are not supported")
            try:
                content = content_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise SandboxError(ErrorCode.BINARY_FILE, "file is not valid UTF-8 text") from exc
            return {
                "ok": True,
                "path": path,
                "content": content,
                "size": response["size"],
                "truncated": response["truncated"],
            }

    def delete_files(self, session_id: str, paths: list[str]) -> dict[str, Any]:
        with self.lease(session_id) as session:
            self._inventory(session)
            response = self.backend.helper(session.docker, "delete", {"paths": paths})
            self._inventory(session)
            return {"ok": True, "deleted": response["deleted"], "missing": response["missing"]}

    def run_javascript(self, session_id: str, entrypoint: str, arguments: list[str], timeout: int) -> dict[str, Any]:
        with self.lease(session_id) as session:
            entries = self._inventory(session)
            entry = next((item for item in entries if item["path"] == entrypoint), None)
            if not entry or entry["type"] != "file":
                raise SandboxError(ErrorCode.FILE_NOT_FOUND, "JavaScript entrypoint does not exist")
            try:
                result = self.backend.run(
                    session.docker,
                    "node",
                    ["--disable-proto=throw", f"/workspace/{entrypoint}", *arguments],
                    timeout,
                    lambda: self._destroy_locked(session),
                )
            except BaseException:
                self._destroy_locked(session)
                raise
            if session_id in self._sessions:
                try:
                    self._inventory(session)
                except SandboxError:
                    self._destroy_locked(session)
                    raise
            response = {"ok": True, **{key: result[key] for key in (
                "exit_code", "stdout", "stderr", "timed_out", "stdout_truncated",
                "stderr_truncated", "duration_ms",
            )}}
            if result["timed_out"]:
                response["termination_reason"] = ErrorCode.TIMEOUT.value
            elif result.get("output_limited"):
                response["termination_reason"] = ErrorCode.OUTPUT_LIMIT_EXCEEDED.value
            return response

    def destroy(self, session_id: str) -> dict[str, Any]:
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            return {"ok": True, "destroyed": False}
        with session.lock:
            self._destroy_locked(session)
        return {"ok": True, "destroyed": True}

    def abort(self, session_id: str) -> None:
        """Remove a container without waiting for an in-flight session lock.

        This is used only by MCP request-cancellation handling. Docker removal
        interrupts any active exec; the normal request thread then observes a
        daemon error and cannot put the session back into the registry.
        """
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is not None:
            self._destroy_locked(session)

    def _destroy_locked(self, session: Session) -> None:
        with self._registry_lock:
            current = self._sessions.get(session.session_id)
            if current is not session:
                return
            session.destroying = True
            pending = self._pending_removals.get(session.session_id)
            if pending is None:
                pending = PendingRemoval(
                    key=session.session_id,
                    docker=session.docker,
                    session=session,
                    lock=session.removal_lock,
                )
                self._pending_removals[session.session_id] = pending
        self._attempt_pending_removal(pending, blocking=True, raise_on_failure=True)

    def _queue_startup_orphans(self) -> None:
        try:
            candidates = self.backend.orphan_candidates(int(time.time()))
        except (AttributeError, SandboxError):
            return
        with self._registry_lock:
            for docker_session in candidates:
                identity = str(getattr(docker_session.container, "id", id(docker_session.container)))
                key = "startup-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                self._pending_removals.setdefault(key, PendingRemoval(key, docker_session, None))
        self._retry_pending_removals()

    def _attempt_pending_removal(
        self,
        pending: PendingRemoval,
        *,
        blocking: bool,
        raise_on_failure: bool,
    ) -> bool:
        if not pending.lock.acquire(blocking=blocking):
            return False
        try:
            with self._registry_lock:
                if self._pending_removals.get(pending.key) is not pending:
                    return True
            try:
                self.backend.destroy(pending.docker)
            except SandboxError:
                if raise_on_failure:
                    raise
                return False
            with self._registry_lock:
                if self._pending_removals.get(pending.key) is pending:
                    self._pending_removals.pop(pending.key, None)
                if pending.session is not None and self._sessions.get(pending.key) is pending.session:
                    self._sessions.pop(pending.key, None)
            return True
        finally:
            pending.lock.release()

    def _retry_pending_removals(self) -> None:
        with self._registry_lock:
            pending = list(self._pending_removals.values())
        for removal in pending:
            try:
                self._attempt_pending_removal(removal, blocking=False, raise_on_failure=False)
            except Exception:
                # Cleanup must never terminate the reaper. The watchdog and a
                # later retry remain available if a backend violates its API.
                _logger.error("unexpected sandbox removal retry failure")

    def _reaper_loop(self) -> None:
        while not self._stop.wait(self.config.cleanup_interval_seconds):
            self._retry_pending_removals()
            with self._registry_lock:
                sessions = list(self._sessions.values())
            now = time.monotonic()
            for session in sessions:
                if session.destroying or not self._expired(session, now) or not session.lock.acquire(blocking=False):
                    continue
                try:
                    if self._expired(session):
                        try:
                            self._destroy_locked(session)
                        except SandboxError:
                            pass
                finally:
                    session.lock.release()

    def close(self) -> None:
        with self._registry_lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
        self._stop.set()
        if self._reaper and self._reaper is not threading.current_thread():
            self._reaper.join(timeout=2)
        for session in sessions:
            with session.lock:
                try:
                    self._destroy_locked(session)
                except SandboxError:
                    pass
        self._retry_pending_removals()
