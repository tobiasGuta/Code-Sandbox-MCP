from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
