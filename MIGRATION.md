# Migration from 0.1

Version 1.0 is intentionally incompatible with the original one-shot `llm-sandbox` API.

## Removed

- `llm-sandbox`, Podman and Kubernetes backends
- `run_python_code` and `run_javascript_code(code=...)`
- `--pass-through-env`, `PASSTHROUGH_ENV`, `CONTAINER_IMAGE`, and `CONTAINER_LANGUAGE`
- custom/model-selected images, language selection, arbitrary libraries, package installation, mutable `latest` images, and network access
- host temporary source files and any implicit current-directory behavior

These features cannot be enabled with compatibility flags. Retaining them would bypass the new trust boundary.

## Replacement workflow

Clients now call `create_sandbox`, `write_files`, `list_files`, `read_file`, `delete_files`, `run_javascript`, and `destroy_sandbox`. JavaScript must already exist below `/workspace` before execution. Every session supports multiple related edits and runs until it is explicitly destroyed, idle, or reaches its absolute lifetime. The optional npm-script feature was deliberately omitted because the distroless release contains no package manager or shell.

Operators must build the reviewed `code-sandbox-mcp-javascript:1.0.0` image before starting the MCP server. The server resolves that tag to an immutable image ID and refuses unlabelled images; it never pulls or accepts an image name from a tool request.

Existing MCP prompts and client code must be updated to use opaque session IDs and structured results/errors. Secrets can no longer be made available to sandboxed programs. Workloads that need Python, outbound networking, arbitrary packages, host repositories, or credentials are intentionally unsupported by this profile.
