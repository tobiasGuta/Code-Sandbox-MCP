# ruff: noqa: S108 - assertions intentionally inspect isolated container tmpfs paths

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from code_sandbox_mcp.config import SandboxConfig
from code_sandbox_mcp.docker_backend import MINIMAL_ENVIRONMENT, DockerBackend
from code_sandbox_mcp.errors import ErrorCode, SandboxError


class FakeDockerContainer:
    def __init__(self):
        self.started = False
        self.removed = False
        self.id = "container-id"
        self.status = "running"
        self.labels = {}
        self.attrs: dict[str, Any] = {}

    def start(self):
        self.started = True

    def remove(self, **kwargs):
        self.removed = True


class FakeContainers:
    def __init__(self):
        self.kwargs: dict[str, Any] | None = None
        self.container = FakeDockerContainer()

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.container

    def list(self, **kwargs):
        assert kwargs == {"all": True, "filters": {"label": "io.code-sandbox-mcp.managed=true"}}
        return [self.container]


def test_container_configuration_is_explicit_and_hardened(tmp_path):
    image = SimpleNamespace(
        id="sha256:" + "a" * 64,
        attrs={"Config": {"Labels": {
            "io.code-sandbox-mcp.profile": "javascript-offline",
            "io.code-sandbox-mcp.runtime-version": "1.0.1",
        }}},
    )
    images = SimpleNamespace(get=lambda reference: image)
    containers = FakeContainers()
    client = SimpleNamespace(images=images, containers=containers)
    config = SandboxConfig(audit_enabled=False, audit_path=tmp_path / "audit")
    backend = DockerBackend(config, client)
    backend.create("b" * 32, 2_000_000_000)
    values = containers.kwargs
    assert values is not None

    assert values["image"] == image.id
    assert values["command"] == ["/opt/sandbox/idle.mjs", "600"]
    assert values["auto_remove"] is True
    assert values["network_mode"] == "none"
    assert values["read_only"] is True
    assert values["user"] == "65532:65532"
    assert values["cap_drop"] == ["ALL"]
    assert values["security_opt"] == ["no-new-privileges:true"]
    assert values["privileged"] is False
    assert values["mem_limit"] == "512m" and values["memswap_limit"] == "512m"
    assert values["nano_cpus"] == 1_000_000_000
    assert values["pids_limit"] == 128
    assert values["init"] is True
    assert values["mounts"] == [] and values["volumes"] == {} and values["devices"] == []
    assert values["ports"] == {} and values["group_add"] == []
    assert set(values["tmpfs"]) == {"/workspace", "/tmp"}
    assert "nosuid" in values["tmpfs"]["/workspace"] and "nodev" in values["tmpfs"]["/workspace"]
    assert "noexec" in values["tmpfs"]["/tmp"]
    assert values["environment"] == MINIMAL_ENVIRONMENT
    assert not any("DOCKER" in key or "TOKEN" in key or "KEY" in key for key in values["environment"])
    assert values["labels"] == {
        "io.code-sandbox-mcp.managed": "true",
        "io.code-sandbox-mcp.owner": "b" * 32,
        "io.code-sandbox-mcp.expires-at": "2000000000",
    }


def test_orphan_discovery_selects_only_expired_or_stopped_managed_containers(tmp_path):
    image = SimpleNamespace(
        id="sha256:" + "a" * 64,
        attrs={"Config": {"Labels": {
            "io.code-sandbox-mcp.profile": "javascript-offline",
            "io.code-sandbox-mcp.runtime-version": "1.0.1",
        }}},
    )
    images = SimpleNamespace(get=lambda reference: image)
    containers = FakeContainers()
    client = SimpleNamespace(images=images, containers=containers)
    backend = DockerBackend(SandboxConfig(audit_enabled=False, audit_path=tmp_path / "audit"), client)

    containers.container.labels = {"io.code-sandbox-mcp.expires-at": "99"}
    assert backend.orphan_candidates(100)[0].container is containers.container
    containers.container.labels = {"io.code-sandbox-mcp.expires-at": "101"}
    assert backend.orphan_candidates(100) == []
    containers.container.status = "exited"
    assert backend.orphan_candidates(100)[0].container is containers.container


@pytest.mark.parametrize("labels", [
    {"io.code-sandbox-mcp.profile": "javascript-offline"},
    {
        "io.code-sandbox-mcp.profile": "javascript-offline",
        "io.code-sandbox-mcp.runtime-version": "1.0.0",
    },
    {
        "io.code-sandbox-mcp.profile": "wrong-profile",
        "io.code-sandbox-mcp.runtime-version": "1.0.1",
    },
])
def test_stale_or_incorrect_runtime_labels_are_rejected(tmp_path, labels):
    image = SimpleNamespace(id="sha256:" + "a" * 64, attrs={"Config": {"Labels": labels}})
    client = SimpleNamespace(images=SimpleNamespace(get=lambda reference: image), containers=FakeContainers())
    config = SandboxConfig(audit_enabled=False, audit_path=tmp_path / "audit")
    with pytest.raises(SandboxError) as caught:
        DockerBackend(config, client)
    assert caught.value.code == ErrorCode.CONTAINER_UNAVAILABLE


def test_old_running_legacy_container_without_expiry_is_cleanup_candidate(tmp_path):
    image = SimpleNamespace(
        id="sha256:" + "a" * 64,
        attrs={"Config": {"Labels": {
            "io.code-sandbox-mcp.profile": "javascript-offline",
            "io.code-sandbox-mcp.runtime-version": "1.0.1",
        }}},
    )
    containers = FakeContainers()
    containers.container.labels = {}
    containers.container.attrs = {"Created": 100}
    client = SimpleNamespace(images=SimpleNamespace(get=lambda reference: image), containers=containers)
    config = SandboxConfig(
        audit_enabled=False,
        audit_path=tmp_path / "audit",
        max_session_lifetime_seconds=600,
    )
    backend = DockerBackend(config, client)

    assert backend.orphan_candidates(699) == []
    assert backend.orphan_candidates(700)[0].container is containers.container


def test_running_legacy_container_with_unknown_creation_time_is_preserved(tmp_path):
    image = SimpleNamespace(
        id="sha256:" + "a" * 64,
        attrs={"Config": {"Labels": {
            "io.code-sandbox-mcp.profile": "javascript-offline",
            "io.code-sandbox-mcp.runtime-version": "1.0.1",
        }}},
    )
    containers = FakeContainers()
    containers.container.labels = {"io.code-sandbox-mcp.expires-at": "invalid"}
    containers.container.attrs = {"Created": "not-a-timestamp"}
    client = SimpleNamespace(images=SimpleNamespace(get=lambda reference: image), containers=containers)
    backend = DockerBackend(SandboxConfig(audit_enabled=False, audit_path=tmp_path / "audit"), client)
    assert backend.orphan_candidates(10_000) == []
