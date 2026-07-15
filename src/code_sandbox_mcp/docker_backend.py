from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import Ulimit

from .config import SANDBOX_RUNTIME_VERSION, SandboxConfig
from .errors import ErrorCode, SandboxError

MINIMAL_ENVIRONMENT = {
    "HOME": "/tmp",  # noqa: S108 - isolated in-container tmpfs, never a host temp path
    "TMPDIR": "/tmp",  # noqa: S108 - isolated in-container tmpfs, never a host temp path
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "NODE_ENV": "production",
}


@dataclass(slots=True)
class DockerSession:
    container: Any


class DockerBackend:
    """Small, auditable adapter around the official Docker Python SDK."""

    def __init__(self, config: SandboxConfig, client: Any | None = None) -> None:
        self.config = config
        try:
            self.client = client or docker.from_env()
            image = self.client.images.get(config.image)
        except ImageNotFound as exc:
            raise SandboxError(
                ErrorCode.CONTAINER_UNAVAILABLE,
                "the approved sandbox image is not installed; build it before starting the server",
            ) from exc
        except DockerException as exc:
            raise SandboxError(ErrorCode.CONTAINER_UNAVAILABLE, "Docker is unavailable") from exc
        labels = (image.attrs.get("Config") or {}).get("Labels") or {}
        if (
            labels.get("io.code-sandbox-mcp.profile") != "javascript-offline"
            or labels.get("io.code-sandbox-mcp.runtime-version") != SANDBOX_RUNTIME_VERSION
        ):
            raise SandboxError(
                ErrorCode.CONTAINER_UNAVAILABLE,
                "the installed image does not have the required runtime labels",
            )
        # Resolve once to an immutable content ID. A later retag cannot change sessions.
        self.image_id = str(image.id)
        self.security_opt = ["no-new-privileges:true"]
        info_method = getattr(self.client, "info", None)
        if callable(info_method):
            try:
                daemon_info: Any = info_method()
                daemon_security = daemon_info.get("SecurityOptions") or []
                if any(option == "name=apparmor" or option.startswith("name=apparmor,") for option in daemon_security):
                    self.security_opt.append("apparmor=docker-default")
            except DockerException:
                # Container creation remains fail-closed; Docker still applies
                # its built-in seccomp/default LSM policy when available.
                pass

    def create(self, owner_label: str, expires_at_epoch: int) -> DockerSession:
        tmpfs = {
            "/workspace": (
                f"rw,nosuid,nodev,size={self.config.max_workspace_bytes},"
                "mode=0700,uid=65532,gid=65532"
            ),
            "/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=0700,uid=65532,gid=65532",  # noqa: S108
        }
        container = None
        try:
            container = self.client.containers.create(
                image=self.image_id,
                command=["/opt/sandbox/idle.mjs", str(self.config.max_session_lifetime_seconds)],
                detach=True,
                auto_remove=True,
                tty=False,
                stdin_open=False,
                network_mode="none",
                read_only=True,
                user="65532:65532",
                cap_drop=["ALL"],
                security_opt=self.security_opt.copy(),
                mem_limit="512m",
                memswap_limit="512m",
                nano_cpus=1_000_000_000,
                pids_limit=128,
                init=True,
                tmpfs=tmpfs,
                environment=MINIMAL_ENVIRONMENT.copy(),
                working_dir="/workspace",
                privileged=False,
                devices=[],
                group_add=[],
                ports={},
                volumes={},
                mounts=[],
                ulimits=[Ulimit(name="core", soft=0, hard=0), Ulimit(name="nofile", soft=1024, hard=1024)],
                labels={
                    "io.code-sandbox-mcp.managed": "true",
                    "io.code-sandbox-mcp.owner": owner_label,
                    "io.code-sandbox-mcp.expires-at": str(expires_at_epoch),
                },
            )
            container.start()
            return DockerSession(container=container)
        except DockerException as exc:
            if container is not None:
                try:
                    container.remove(force=True, v=True)
                except DockerException:
                    pass
            raise SandboxError(ErrorCode.CONTAINER_START_FAILED, "sandbox container could not be started") from exc

    def orphan_candidates(self, now_epoch: int) -> list[DockerSession]:
        """Return stopped or expired managed containers without touching live peers."""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "io.code-sandbox-mcp.managed=true"},
            )
        except DockerException as exc:
            raise SandboxError(ErrorCode.CONTAINER_UNAVAILABLE, "managed containers could not be enumerated") from exc

        candidates: list[DockerSession] = []
        for container in containers:
            labels = getattr(container, "labels", None)
            if not isinstance(labels, dict):
                labels = ((getattr(container, "attrs", {}) or {}).get("Config") or {}).get("Labels") or {}
            raw_expiry = labels.get("io.code-sandbox-mcp.expires-at")
            expired = False
            if isinstance(raw_expiry, str) and raw_expiry.isascii() and raw_expiry.isdecimal():
                expired = int(raw_expiry) <= now_epoch
            legacy_expired = False
            if not isinstance(raw_expiry, str) or not raw_expiry.isascii() or not raw_expiry.isdecimal():
                created_epoch = self._container_created_epoch(container)
                if created_epoch is not None:
                    legacy_expired = created_epoch + self.config.max_session_lifetime_seconds <= now_epoch
            status = str(getattr(container, "status", "")).lower()
            if expired or legacy_expired or status not in {"running", "restarting"}:
                candidates.append(DockerSession(container))
        return candidates

    @staticmethod
    def _container_created_epoch(container: Any) -> int | None:
        raw_created = (getattr(container, "attrs", {}) or {}).get("Created")
        if isinstance(raw_created, int | float):
            return int(raw_created)
        if not isinstance(raw_created, str) or not raw_created:
            return None
        try:
            parsed = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())

    def destroy(self, session: DockerSession) -> None:
        try:
            session.container.remove(force=True, v=True)
        except NotFound:
            return
        except DockerException:
            try:
                session.container.stop(timeout=1)
                session.container.remove(force=True, v=True)
            except NotFound:
                return
            except DockerException as exc:
                raise SandboxError(
                    ErrorCode.CONTAINER_REMOVAL_FAILED,
                    "sandbox container could not be removed",
                ) from exc

    @staticmethod
    def _encode_request(request: dict[str, Any]) -> str:
        raw = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _exec_json(self, session: DockerSession, command: list[str]) -> dict[str, Any]:
        try:
            result = session.container.exec_run(
                command,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                privileged=False,
                user="65532:65532",
                workdir="/workspace",
                environment=MINIMAL_ENVIRONMENT.copy(),
                demux=True,
            )
        except DockerException as exc:
            raise SandboxError(ErrorCode.INTERNAL_ERROR, "sandbox operation failed") from exc
        stdout, _stderr = result.output
        try:
            response = json.loads((stdout or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError(ErrorCode.INTERNAL_ERROR, "sandbox returned an invalid response") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            try:
                code = ErrorCode(error.get("code", ErrorCode.INTERNAL_ERROR.value))
            except ValueError:
                code = ErrorCode.INTERNAL_ERROR
            raise SandboxError(code, str(error.get("message", "sandbox operation failed")))
        return response

    def helper(self, session: DockerSession, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        encoded = self._encode_request(request)
        return self._exec_json(session, ["node", "/opt/sandbox/helper.mjs", operation, encoded])

    def put_files(self, session: DockerSession, files: list[tuple[str, bytes]]) -> None:
        request = {"files": [{"path": path, "size": len(content)} for path, content in files]}
        command = ["node", "/opt/sandbox/helper.mjs", "write", self._encode_request(request)]
        payload = b"".join(content for _path, content in files)
        stream_socket = None
        try:
            created = self.client.api.exec_create(
                session.container.id,
                command,
                stdout=True,
                stderr=True,
                stdin=True,
                tty=False,
                privileged=False,
                user="65532:65532",
                workdir="/workspace",
                environment=MINIMAL_ENVIRONMENT.copy(),
            )
            exec_id = created["Id"]
            stream_socket = self.client.api.exec_start(exec_id, tty=False, socket=True)
            raw_socket = getattr(stream_socket, "_sock", stream_socket)
            raw_socket.sendall(payload)
            try:
                raw_socket.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            stream_socket.close()
            stream_socket = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                inspection = self.client.api.exec_inspect(exec_id)
                if not inspection.get("Running", False):
                    if inspection.get("ExitCode") != 0:
                        raise SandboxError(ErrorCode.INTERNAL_ERROR, "file writer rejected the transfer")
                    return
                time.sleep(0.01)
            raise SandboxError(ErrorCode.TIMEOUT, "file transfer timed out")
        except DockerException as exc:
            raise SandboxError(ErrorCode.INTERNAL_ERROR, "file transfer failed") from exc
        except OSError as exc:
            raise SandboxError(ErrorCode.INTERNAL_ERROR, "file transfer failed") from exc
        finally:
            if stream_socket is not None:
                stream_socket.close()

    def run(
        self,
        session: DockerSession,
        executable: str,
        arguments: list[str],
        timeout_seconds: int,
        on_host_timeout: Callable[[], None],
    ) -> dict[str, Any]:
        request = {
            "executable": executable,
            "args": arguments,
            "environment": MINIMAL_ENVIRONMENT,
            "timeout_ms": timeout_seconds * 1000,
            "stdout_limit": self.config.max_stdout_bytes,
            "stderr_limit": self.config.max_stderr_bytes,
        }
        encoded = self._encode_request(request)
        command = ["node", "/opt/sandbox/runner.mjs", encoded]
        response: list[dict[str, Any]] = []
        failure: list[BaseException] = []

        def target() -> None:
            try:
                response.append(self._exec_json(session, command))
            except BaseException as exc:  # passed back to the request thread
                failure.append(exc)

        thread = threading.Thread(target=target, name="sandbox-exec", daemon=True)
        thread.start()
        thread.join(timeout_seconds + 5)
        if thread.is_alive():
            on_host_timeout()
            thread.join(2)
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "output_limited": False,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "duration_ms": (timeout_seconds + 5) * 1000,
            }
        if failure:
            error = failure[0]
            if isinstance(error, SandboxError):
                raise error
            raise SandboxError(ErrorCode.INTERNAL_ERROR, "sandbox execution failed") from error
        return response[0]
