from __future__ import annotations

import os

import pytest
from docker.errors import NotFound

from code_sandbox_mcp.audit import AuditLogger
from code_sandbox_mcp.config import SandboxConfig
from code_sandbox_mcp.docker_backend import MINIMAL_ENVIRONMENT, DockerBackend
from code_sandbox_mcp.errors import ErrorCode, SandboxError
from code_sandbox_mcp.session import SessionManager

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="set RUN_DOCKER_TESTS=1 after building the approved sandbox image",
)


@pytest.fixture
def docker_manager(tmp_path):
    config = SandboxConfig(audit_enabled=True, audit_path=tmp_path / "audit.jsonl")
    manager = SessionManager(config, DockerBackend(config), AuditLogger(True, config.audit_path))
    yield manager
    manager.close()


def test_real_container_workflow_and_security_inspection(docker_manager):
    session_id = docker_manager.create()["session_id"]
    docker_manager.write_files(session_id, [{"path": "index.js", "content": "console.log('hello')"}], False)
    assert docker_manager.run_javascript(session_id, "index.js", [], 20)["stdout"] == "hello\n"
    session = docker_manager._sessions[session_id]
    session.docker.container.reload()
    attrs = session.docker.container.attrs
    host = attrs["HostConfig"]
    assert attrs["Config"]["User"] == "65532:65532"
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Privileged"] is False
    assert host["Memory"] == 512 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 128
    assert host["Binds"] in (None, [])
    assert host["PidMode"] in ("", "private")
    assert host["IpcMode"] in ("", "private")
    assert host["Devices"] in (None, [])
    assert host["PortBindings"] in (None, {})
    environment = dict(item.split("=", 1) for item in attrs["Config"]["Env"])
    assert {key: environment[key] for key in MINIMAL_ENVIRONMENT} == MINIMAL_ENVIRONMENT
    assert set(environment) - set(MINIMAL_ENVIRONMENT) <= {"SSL_CERT_FILE"}
    assert not any(key.upper().endswith(("TOKEN", "SECRET", "PASSWORD", "API_KEY")) for key in environment)
    assert attrs["Mounts"] == []
    container = session.docker.container
    socket_check = container.exec_run([
        "node", "-e", "process.exit(require('fs').existsSync('/var/run/docker.sock') ? 1 : 0)",
    ])
    windows_mount_check = container.exec_run([
        "node", "-e", "process.exit(require('fs').existsSync('/mnt/c') ? 1 : 0)",
    ])
    assert socket_check.exit_code == 0
    assert windows_mount_check.exit_code == 0
    docker_manager.destroy(session_id)
    with pytest.raises(NotFound):
        container.reload()


@pytest.mark.parametrize("source", [
    "require('fs').writeFileSync('/etc/test', 'x')",
    "require('child_process').execSync('mount')",
    "fetch('https://example.com').then(()=>process.exit(0)).catch(()=>process.exit(7))",
    "fetch('http://host.docker.internal').then(()=>process.exit(0)).catch(()=>process.exit(7))",
    "fetch('http://169.254.169.254').then(()=>process.exit(0)).catch(()=>process.exit(7))",
    "fetch('http://172.17.0.1').then(()=>process.exit(0)).catch(()=>process.exit(7))",
    "fetch('http://127.0.0.1:1').then(()=>process.exit(0)).catch(()=>process.exit(7))",
])
def test_adversarial_operations_fail(docker_manager, source):
    session_id = docker_manager.create()["session_id"]
    docker_manager.write_files(session_id, [{"path": "attack.js", "content": source}], False)
    result = docker_manager.run_javascript(session_id, "attack.js", [], 10)
    assert result["exit_code"] != 0


def test_timeout_output_and_workspace_limits(docker_manager):
    session_id = docker_manager.create()["session_id"]
    docker_manager.write_files(session_id, [{"path": "loop.js", "content": "while (true) {}"}], False)
    result = docker_manager.run_javascript(session_id, "loop.js", [], 1)
    assert result["timed_out"] is True
    assert result["termination_reason"] == "TIMEOUT"

    docker_manager.write_files(
        session_id,
        [{"path": "output.js", "content": "process.stdout.write('x'.repeat(2 * 1024 * 1024))"}],
        False,
    )
    result = docker_manager.run_javascript(session_id, "output.js", [], 10)
    assert result["stdout_truncated"] is True, result
    assert result["termination_reason"] == "OUTPUT_LIMIT_EXCEEDED"


def test_container_system_file_is_container_only(docker_manager):
    session_id = docker_manager.create()["session_id"]
    source = "console.log(require('fs').readFileSync('/etc/passwd', 'utf8'))"
    docker_manager.write_files(session_id, [{"path": "read-system.js", "content": source}], False)
    result = docker_manager.run_javascript(session_id, "read-system.js", [], 10)
    assert "nonroot:x:65532:65532" in result["stdout"]
    assert "docker.sock" not in result["stdout"]


def test_large_file_stays_in_tmpfs(docker_manager):
    session_id = docker_manager.create()["session_id"]
    content = "x" * (2 * 1024 * 1024)
    docker_manager.write_files(session_id, [{"path": "data/large.txt", "content": content}], False)
    result = docker_manager.read_file(session_id, "data/large.txt", 64)
    assert result["content"] == "x" * 64
    assert result["size"] == len(content)
    assert result["truncated"] is True


@pytest.mark.parametrize("source", [
    "require('fs').symlinkSync('/etc/passwd', '/workspace/escape')",
    "const fs=require('fs');fs.writeFileSync('/workspace/a','x');fs.linkSync('/workspace/a','/workspace/b')",
])
def test_links_invalidate_and_destroy_session(docker_manager, source):
    session_id = docker_manager.create()["session_id"]
    docker_manager.write_files(session_id, [{"path": "links.js", "content": source}], False)
    with pytest.raises(SandboxError) as caught:
        docker_manager.run_javascript(session_id, "links.js", [], 10)
    assert caught.value.code == ErrorCode.UNSAFE_FILE_TYPE
    assert session_id not in docker_manager._sessions


@pytest.mark.destructive_docker
@pytest.mark.skipif(
    os.environ.get("RUN_DESTRUCTIVE_DOCKER_TESTS") != "1",
    reason="set RUN_DESTRUCTIVE_DOCKER_TESTS=1 for constrained resource-exhaustion tests",
)
@pytest.mark.parametrize("source", [
    "const fs=require('fs');try{fs.writeFileSync('/workspace/fill',Buffer.alloc(70*1024*1024))}catch{process.exit(7)}",
    "const a=[];try{while(true)a.push(Buffer.alloc(16*1024*1024))}catch{process.exit(7)}",
    "const {spawn}=require('child_process');for(let i=0;i<256;i++){try{"
    "spawn('node',['-e','setInterval(()=>{},1e6)'])}catch{}}",
])
def test_resource_exhaustion_is_bounded_and_cleaned(docker_manager, source):
    session_id = docker_manager.create()["session_id"]
    docker_manager.write_files(session_id, [{"path": "resource.js", "content": source}], False)
    try:
        result = docker_manager.run_javascript(session_id, "resource.js", [], 15)
        assert result["exit_code"] != 0 or result["duration_ms"] < 20_000
    except SandboxError:
        pass
    finally:
        docker_manager.destroy(session_id)
