from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from code_sandbox_mcp.docker_backend import DockerSession
from code_sandbox_mcp.errors import ErrorCode, SandboxError


@dataclass
class FakeContainer:
    files: dict[str, bytes] = field(default_factory=dict)
    removed: bool = False
    destroy_failures: int = 0
    expires_at_epoch: int | None = None


class FakeBackend:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.created: list[FakeContainer] = []
        self.startup_orphans: list[FakeContainer] = []
        self.last_run: tuple[str, list[str], int] | None = None

    def create(self, owner_label: str, expires_at_epoch: int) -> DockerSession:
        assert len(owner_label) == 32
        container = FakeContainer(expires_at_epoch=expires_at_epoch)
        self.created.append(container)
        return DockerSession(container)

    def destroy(self, session: DockerSession) -> None:
        if session.container.destroy_failures:
            session.container.destroy_failures -= 1
            raise SandboxError(ErrorCode.CONTAINER_REMOVAL_FAILED, "simulated removal failure")
        session.container.removed = True

    def orphan_candidates(self, now_epoch: int) -> list[DockerSession]:
        del now_epoch
        return [DockerSession(container) for container in self.startup_orphans if not container.removed]

    @staticmethod
    def _entries(container: FakeContainer) -> list[dict[str, Any]]:
        directories: set[str] = set()
        for name in container.files:
            parent = PurePosixPath(name).parent
            while parent.as_posix() != ".":
                directories.add(parent.as_posix())
                parent = parent.parent
        entries = [{"path": name, "type": "directory", "size": 0, "links": 1} for name in directories]
        entries.extend(
            {"path": name, "type": "file", "size": len(content), "links": 1}
            for name, content in container.files.items()
        )
        return sorted(entries, key=lambda item: item["path"])

    def helper(self, session: DockerSession, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        container = session.container
        entries = self._entries(container)
        if operation == "inventory":
            limit = request["limit"]
            return {"ok": True, "entries": entries[:limit], "truncated": len(entries) >= limit}
        if operation == "list":
            base = request["path"]
            if base != "." and not any(item["path"] == base for item in entries):
                raise SandboxError(ErrorCode.FILE_NOT_FOUND, "workspace path does not exist")
            prefix = "" if base == "." else base + "/"
            selected = []
            for item in entries:
                if base != "." and item["path"] == base:
                    selected.append(item)
                    continue
                if not item["path"].startswith(prefix):
                    continue
                remainder = item["path"][len(prefix):]
                depth = remainder.count("/")
                recursive_match = request["recursive"] and depth <= request["max_depth"]
                direct_match = not request["recursive"] and depth == 0
                if recursive_match or direct_match:
                    selected.append(item)
            limit = request["limit"]
            return {"ok": True, "entries": selected[:limit], "truncated": len(selected) > limit}
        if operation == "read":
            try:
                content = container.files[request["path"]]
            except KeyError as exc:
                raise SandboxError(ErrorCode.FILE_NOT_FOUND, "workspace path does not exist") from exc
            maximum = request["max_bytes"]
            return {
                "ok": True,
                "content_base64": base64.b64encode(content[:maximum]).decode(),
                "size": len(content),
                "truncated": len(content) > maximum,
            }
        if operation == "delete":
            deleted, missing = [], []
            for name in request["paths"]:
                if name in container.files:
                    del container.files[name]
                    deleted.append(name)
                else:
                    missing.append(name)
            return {"ok": True, "deleted": deleted, "missing": missing}
        raise AssertionError(operation)

    def put_files(self, session: DockerSession, files: list[tuple[str, bytes]]) -> None:
        for name, content in files:
            session.container.files[name] = content

    def run(
        self,
        session: DockerSession,
        executable: str,
        arguments: list[str],
        timeout_seconds: int,
        on_host_timeout: Any,
    ) -> dict[str, Any]:
        self.last_run = (executable, arguments, timeout_seconds)
        assert executable == "node"
        relative = arguments[1].removeprefix("/workspace/")
        source = session.container.files[relative].decode()
        timed_out = "while (true)" in source
        stdout = "hello\n" if "console.log" in source else ""
        stderr = "problem\n" if "console.error" in source else ""
        exit_code = 124 if timed_out else (7 if "process.exit(7)" in source else 0)
        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "output_limited": False,
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "duration_ms": 10,
        }
