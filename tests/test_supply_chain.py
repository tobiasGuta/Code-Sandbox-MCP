from __future__ import annotations

import re
from pathlib import Path

from code_sandbox_mcp.config import PINNED_SANDBOX_IMAGE, SANDBOX_RUNTIME_VERSION

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"


def test_every_github_action_is_pinned_to_a_full_commit_sha():
    workflow = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", workflow, flags=re.MULTILINE)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_package_and_image_metadata_point_to_hardened_repository():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "containers" / "Dockerfile.nodejs").read_text(encoding="utf-8")
    expected = "https://github.com/tobiasGuta/Code-Sandbox-MCP"
    assert project.count(expected) == 3
    assert f'org.opencontainers.image.source="{expected}"' in dockerfile


def test_package_runtime_image_ci_and_readme_versions_match():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "containers" / "Dockerfile.nodejs").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    project_versions = re.findall(rf'^version\s*=\s*"({VERSION_PATTERN})"\s*$', project, re.MULTILINE)
    runtime_labels = re.findall(
        rf'io\.code-sandbox-mcp\.runtime-version="({VERSION_PATTERN})"',
        dockerfile,
    )
    workflow_tags = re.findall(rf"code-sandbox-mcp-javascript:({VERSION_PATTERN})", workflow)
    readme_tags = re.findall(rf"code-sandbox-mcp-javascript:({VERSION_PATTERN})", readme)

    assert len(project_versions) == 1, "pyproject.toml must contain exactly one project version"
    assert len(runtime_labels) == 1, "Dockerfile must contain exactly one runtime-version label"
    assert workflow_tags, "security workflow must reference the versioned sandbox image"
    assert readme_tags, "README build commands must reference the versioned sandbox image"

    expected = project_versions[0]
    observed = {
        "SANDBOX_RUNTIME_VERSION": SANDBOX_RUNTIME_VERSION,
        "PINNED_SANDBOX_IMAGE": PINNED_SANDBOX_IMAGE.removeprefix("code-sandbox-mcp-javascript:"),
        "Dockerfile runtime label": runtime_labels[0],
        "security.yml image tags": set(workflow_tags),
        "README image tags": set(readme_tags),
    }
    assert observed == {
        "SANDBOX_RUNTIME_VERSION": expected,
        "PINNED_SANDBOX_IMAGE": expected,
        "Dockerfile runtime label": expected,
        "security.yml image tags": {expected},
        "README image tags": {expected},
    }
