from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_SESSION = "INVALID_SESSION"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    INVALID_PATH = "INVALID_PATH"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    WORKSPACE_LIMIT_EXCEEDED = "WORKSPACE_LIMIT_EXCEEDED"
    BINARY_FILE = "BINARY_FILE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    INVALID_REQUEST = "INVALID_REQUEST"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    CONTAINER_START_FAILED = "CONTAINER_START_FAILED"
    CONTAINER_REMOVAL_FAILED = "CONTAINER_REMOVAL_FAILED"
    CONTAINER_UNAVAILABLE = "CONTAINER_UNAVAILABLE"
    PROCESS_CLEANUP_FAILED = "PROCESS_CLEANUP_FAILED"
    UNSAFE_FILE_TYPE = "UNSAFE_FILE_TYPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SandboxError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"ok": False, "error": {"code": self.code.value, "message": self.safe_message}}
