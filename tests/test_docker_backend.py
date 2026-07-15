# ruff: noqa: S108 - assertions intentionally inspect isolated container tmpfs paths

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from code_sandbox_mcp.config import SandboxConfig
from code_sandbox_mcp.docker_backend import MINIMAL_ENVIRONMENT, DockerBackend


class FakeDockerContainer:
    def __init__(self):
        self.started = False
        self.removed = False
        self.id = "container-id"
        self.status = "running"
        self.labels = {}

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
        attrs={"Config": {"Labels": {"io.code-sandbox-mcp.profile": "javascript-offline"}}},
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
        attrs={"Config": {"Labels": {"io.code-sandbox-mcp.profile": "javascript-offline"}}},
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
