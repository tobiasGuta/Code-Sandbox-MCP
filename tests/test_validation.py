from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_sandbox_mcp.errors import ErrorCode, SandboxError
from code_sandbox_mcp.models import (
    DeleteFilesRequest,
    FileWrite,
    RunJavascriptRequest,
    WriteFilesRequest,
    validate_workspace_path,
)


@pytest.mark.parametrize(
    "path,code",
    [
        ("../secret", ErrorCode.PATH_TRAVERSAL),
        ("../../etc/passwd", ErrorCode.PATH_TRAVERSAL),
        ("/etc/passwd", ErrorCode.INVALID_PATH),
        ("/proc/self/environ", ErrorCode.INVALID_PATH),
        ("/sys/kernel", ErrorCode.INVALID_PATH),
        ("/dev/null", ErrorCode.INVALID_PATH),
        (r"C:\Users\User\.ssh\id_rsa", ErrorCode.INVALID_PATH),
        (r"\\server\share\file", ErrorCode.INVALID_PATH),
        ("folder/../../../secret", ErrorCode.PATH_TRAVERSAL),
        (r"folder\..\..\secret", ErrorCode.PATH_TRAVERSAL),
        ("file.txt\x00.js", ErrorCode.INVALID_PATH),
        ("%2e%2e/secret", ErrorCode.PATH_TRAVERSAL),
        ("%252e%252e%252fsecret", ErrorCode.PATH_TRAVERSAL),
        ("folder\\../secret", ErrorCode.PATH_TRAVERSAL),
        ("NUL.txt", ErrorCode.INVALID_PATH),
        ("file.txt:stream", ErrorCode.INVALID_PATH),
    ],
)
def test_rejects_dangerous_paths(path, code):
    with pytest.raises(SandboxError) as caught:
        validate_workspace_path(path)
    assert caught.value.code == code


def test_accepts_normalized_relative_paths():
    assert validate_workspace_path("test/parser.test.js") == "test/parser.test.js"


def test_rejects_excessive_path_depth_and_length():
    with pytest.raises(SandboxError):
        validate_workspace_path("/".join(["x"] * 21))
    with pytest.raises(SandboxError):
        validate_workspace_path("x" * 513)


def test_models_forbid_extra_fields_and_duplicates():
    session_id = "a" * 43
    with pytest.raises(ValidationError):
        FileWrite.model_validate({"path": "x.js", "content": "", "mode": 0o777})
    with pytest.raises(ValidationError):
        WriteFilesRequest(session_id=session_id, files=[
            FileWrite(path="x.js", content="a"),
            FileWrite(path="x.js", content="b"),
        ])
    with pytest.raises(ValidationError):
        DeleteFilesRequest(session_id=session_id, paths=["x.js", "x.js"])


def test_execution_models_reject_free_form_or_lifecycle_inputs():
    session_id = "a" * 43
    with pytest.raises(ValidationError):
        RunJavascriptRequest(session_id=session_id, entrypoint="index.py")
    with pytest.raises(ValidationError):
        RunJavascriptRequest(session_id=session_id, entrypoint="index.js", arguments=["x" * 4097])
