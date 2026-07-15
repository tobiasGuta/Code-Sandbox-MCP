from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .errors import ErrorCode, SandboxError

MAX_PATH_LENGTH = 512
MAX_PATH_DEPTH = 20
MAX_ARGUMENTS = 32
MAX_ARGUMENT_LENGTH = 4096
MAX_REQUEST_BYTES = 8 * 1024 * 1024
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
WINDOWS_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)

StrictText = Annotated[str, StringConstraints(strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def validate_workspace_path(raw: str, *, allow_dot: bool = False) -> str:
    if not isinstance(raw, str) or not raw:
        raise SandboxError(ErrorCode.INVALID_PATH, "path must be a non-empty string")
    if "\x00" in raw:
        raise SandboxError(ErrorCode.INVALID_PATH, "path contains a null byte")
    if len(raw.encode("utf-8")) > MAX_PATH_LENGTH:
        raise SandboxError(ErrorCode.INVALID_PATH, "path is too long")
    if raw.startswith(("/", "\\", "//")) or PureWindowsPath(raw).is_absolute():
        raise SandboxError(ErrorCode.INVALID_PATH, "absolute, drive, and UNC paths are forbidden")
    normalized_slashes = raw.replace("\\", "/")
    decoded = unquote(unquote(normalized_slashes)).replace("\\", "/")
    if decoded != normalized_slashes and any(part == ".." for part in decoded.split("/")):
        raise SandboxError(ErrorCode.PATH_TRAVERSAL, "encoded parent traversal is forbidden")
    parts = normalized_slashes.split("/")
    if any(part == ".." for part in parts):
        raise SandboxError(ErrorCode.PATH_TRAVERSAL, "parent traversal is forbidden")
    if any(part in {"", "."} for part in parts):
        if allow_dot and normalized_slashes == ".":
            return "."
        raise SandboxError(ErrorCode.INVALID_PATH, "empty and dot path components are forbidden")
    if len(parts) > MAX_PATH_DEPTH:
        raise SandboxError(ErrorCode.INVALID_PATH, "path exceeds the maximum directory depth")
    # Colons reject drive/device syntax and alternate data stream names on Windows.
    if any(":" in part for part in parts):
        raise SandboxError(ErrorCode.INVALID_PATH, "device and alternate-stream paths are forbidden")
    if any(WINDOWS_DEVICE_NAME.fullmatch(part.rstrip(" .")) for part in parts):
        raise SandboxError(ErrorCode.INVALID_PATH, "reserved device paths are forbidden")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized in {"", "."} and not allow_dot:
        raise SandboxError(ErrorCode.INVALID_PATH, "workspace root is not a file path")
    return normalized


def _path_validator(value: str) -> str:
    return validate_workspace_path(value)


class SessionRequest(StrictModel):
    session_id: StrictText = Field(min_length=32, max_length=128)

    @field_validator("session_id")
    @classmethod
    def valid_session_id(cls, value: str) -> str:
        if not SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid session identifier")
        return value


class FileWrite(StrictModel):
    path: StrictText
    content: StrictText

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _path_validator(value)


class WriteFilesRequest(SessionRequest):
    files: list[FileWrite] = Field(min_length=1, max_length=100)
    overwrite: bool = False

    @model_validator(mode="after")
    def request_limits(self) -> WriteFilesRequest:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate file paths are forbidden")
        request_bytes = sum(len(item.path.encode()) + len(item.content.encode()) for item in self.files)
        if request_bytes > MAX_REQUEST_BYTES:
            raise ValueError("request is too large")
        return self


class ListFilesRequest(SessionRequest):
    path: StrictText = "."
    recursive: bool = True
    max_depth: int = Field(default=5, ge=0, le=10)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return validate_workspace_path(value, allow_dot=True)


class ReadFileRequest(SessionRequest):
    path: StrictText
    max_bytes: int = Field(default=65536, ge=1, le=2 * 1024 * 1024)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _path_validator(value)


class DeleteFilesRequest(SessionRequest):
    paths: list[StrictText] = Field(min_length=1, max_length=100)

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, values: list[str]) -> list[str]:
        normalized = [_path_validator(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate file paths are forbidden")
        return normalized


class RunJavascriptRequest(SessionRequest):
    entrypoint: StrictText
    arguments: list[StrictText] = Field(default_factory=list, max_length=MAX_ARGUMENTS)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)

    @field_validator("entrypoint")
    @classmethod
    def valid_entrypoint(cls, value: str) -> str:
        value = _path_validator(value)
        if not value.endswith((".js", ".mjs", ".cjs")):
            raise ValueError("entrypoint must be a JavaScript file")
        return value

    @field_validator("arguments")
    @classmethod
    def valid_arguments(cls, values: list[str]) -> list[str]:
        for value in values:
            if "\x00" in value or len(value.encode()) > MAX_ARGUMENT_LENGTH:
                raise ValueError("argument is invalid or too long")
        return values

