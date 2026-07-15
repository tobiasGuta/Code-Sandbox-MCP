# Code Sandbox MCP

This project is an improved and security-hardened version of [philschmid/code-sandbox-mcp](https://github.com/philschmid/code-sandbox-mcp).

Code Sandbox MCP provides short-lived, offline JavaScript workspaces to stdio MCP clients such as Codex. A client can create a sandbox, write and enumerate several files, read or delete files, run a JavaScript entrypoint, and destroy the session. Submitted source is streamed from memory into a container; it is never written to the repository, a host temporary directory, or a host bind mount.

This is a human-directed execution helper, not an autonomous agent. It has no target discovery, browser control, credential access, network-enabled profile, or general shell tool.

## Security model

The MCP process is trusted and can talk to the host Docker daemon through the official Docker Python SDK. On Windows, `docker.from_env()` uses Docker Desktop's normal named-pipe configuration. The sandbox container does **not** receive that pipe, a Docker or Podman socket, a host path, the server environment, a device, a port, or a host namespace.

Each session is one Linux container with:

- an immutable local image ID resolved from the single server allowlisted tag;
- UID/GID `65532:65532`, all capabilities dropped, `no-new-privileges`, Docker's built-in seccomp policy, and `docker-default` AppArmor when the daemon reports AppArmor support;
- `network_mode=none`, no published ports, and no DNS configuration;
- a read-only root filesystem;
- a 64 MiB `tmpfs` at `/workspace` (`rw,nosuid,nodev,mode=0700`);
- a 64 MiB `tmpfs` at `/tmp` (`rw,noexec,nosuid,nodev,mode=0700`);
- 512 MiB memory and swap limits, one CPU, 128 PIDs, an init process, no devices, no extra groups, and no mounts;
- only a fixed, minimal runtime environment plus the distroless image's non-secret certificate-file path. Host environment variables and credentials are never copied.

Docker isolation substantially reduces risk, but it is not a VM or a perfect security boundary. The Docker daemon and host kernel remain in the trusted computing base. Do not use this project to analyze container-escape exploits, hostile kernel-level malware, or workloads that require protection from a compromised Docker daemon. Keep Docker Desktop and the host patched, and do not share one stdio server process among unrelated clients.

Networking is deliberately unavailable. Sandboxed code cannot reach the internet, LAN, cloud metadata, `host.docker.internal`, Docker Desktop services, or host localhost services. There is no supported flag to change this. Dependencies must be baked into a reviewed image; the default distroless image provides Node.js and its built-in modules only. It contains no npm, package manager, or shell, and the MCP surface has no install or npm-script tool.

## Session lifecycle and limits

`create_sandbox` returns a cryptographically random opaque ID. Raw Docker IDs are never returned. Sessions belong to the MCP server process that created them and are held only in memory.

| Limit | Default |
|---|---:|
| Absolute lifetime | 10 minutes |
| Inactivity timeout | 3 minutes |
| Concurrent sessions | 3 |
| Workspace entries | 100 |
| Individual file | 2 MiB |
| Workspace tmpfs | 64 MiB |
| stdout / stderr | 1 MiB each |
| Command timeout | 30 seconds |
| Maximum command timeout | 120 seconds |

A background reaper destroys expired sessions. `destroy_sandbox` force-removes a container and is idempotent. All sessions are removed from an `atexit` handler and from the server's `finally` block when stdio closes. Cancelling `run_javascript` asynchronously aborts the container without waiting for its session lock. Execution timeout or an unexpected execution/file-transfer failure also removes the affected session. Output limits are counted as bytes inside the container; the process group is killed and the response reports `OUTPUT_LIMIT_EXCEEDED` when a stream crosses its cap.

Server operators may lower or raise bounded limits with `CODE_SANDBOX_MAX_LIFETIME`, `CODE_SANDBOX_IDLE_TIMEOUT`, `CODE_SANDBOX_MAX_SESSIONS`, `CODE_SANDBOX_DEFAULT_TIMEOUT`, and `CODE_SANDBOX_MAX_TIMEOUT`. Values are strictly validated at startup. These settings are not MCP inputs.

## Run with Docker Desktop on Windows

### How the two processes work

The MCP server itself runs as a local Python process. It uses the Docker SDK to create and remove a separate locked-down Linux container for each sandbox session:

```text
Codex / Claude Code / Gemini CLI
              |
              | MCP over stdio
              v
code-sandbox-mcp.exe on Windows
              |
              | Docker Desktop API
              v
short-lived offline sandbox container
```

Do **not** start the sandbox image with `docker run`. The MCP server must create it so all security settings, limits, labels, tmpfs mounts, and cleanup behavior are applied together.

### Prerequisites

- Windows 10 or 11;
- Docker Desktop running **Linux containers**;
- Python 3.10 or newer; and
- a local checkout of this repository.

Confirm Docker Desktop is ready:

```powershell
docker version
docker info --format '{{.OSType}}'
```

The second command must print `linux`.

### Install the MCP server

Run these commands from PowerShell:

```powershell
Set-Location D:\Tools\code-sandbox-mcp

py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
```

The final command must be executed with `.venv\Scripts\python.exe`. Installing with a different Python puts the package in that Python installation and does not create the launcher inside this virtual environment.

Verify the launcher:

```powershell
Test-Path .\.venv\Scripts\code-sandbox-mcp.exe
& .\.venv\Scripts\python.exe -c "from code_sandbox_mcp.server import main; print('MCP import OK')"
```

`Test-Path` must print `True`.

### Build the sandbox image

```powershell
docker build --pull `
  -t code-sandbox-mcp-javascript:1.0.0 `
  -f containers\Dockerfile.nodejs .
```

Verify the exact local image and its security profile label:

```powershell
docker image inspect code-sandbox-mcp-javascript:1.0.0 --format 'ID={{.Id}}'
docker image inspect code-sandbox-mcp-javascript:1.0.0 --format '{{json .Config.Labels}}'
```

The labels JSON must contain `"io.code-sandbox-mcp.profile":"javascript-offline"`. The Dockerfile copies the Node 22.23.0 binary from a digest-pinned official Node build stage into a separately digest-pinned `distroless/cc-debian12:nonroot` runtime. Build tools, npm, Perl, and the source image filesystem do not enter the final image. The MCP server resolves `code-sandbox-mcp-javascript:1.0.0` to its immutable local `sha256:` image ID and checks the profile label before creating a session. It never pulls an image automatically.

Launching `code-sandbox-mcp.exe` manually is not normally useful: it is a stdio server and waits for an MCP client on standard input. Register the executable with one of the clients below instead.

### Rebuild after container-file changes

Changes to `containers/Dockerfile.nodejs`, `containers/idle.mjs`, `containers/sandbox-helper.mjs`, or `containers/sandbox-runner.mjs` do not affect an already-built image. Rebuild it with the same fixed tag:

```powershell
docker build --pull --no-cache `
  -t code-sandbox-mcp-javascript:1.0.0 `
  -f containers\Dockerfile.nodejs .
```

Existing sandbox sessions continue using their original immutable image ID. Destroy them and create new sessions after rebuilding.

### Hash-locked development installation

For the checked-in Windows/Python 3.14 development environment, install `pylock.toml` with a pip release that supports PEP 751 lock files, then install the local package without resolving anything else:

```powershell
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r pylock.toml
& .\.venv\Scripts\python.exe -m pip install -e . --no-deps --no-build-isolation
```

`pylock.toml` is platform-specific. Other Python/platform combinations use the exactly pinned inputs in `requirements-dev.in` and should generate and review their own lock with `python -m pip lock -r requirements-dev.in -o pylock.toml`.

## Add the server to an MCP CLI

Use the absolute launcher path so clients work regardless of their current directory:

```text
D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe
```

The server intentionally supports local stdio only. Each client starts its own MCP server process, which gives sessions a clear owner and makes client-disconnect cleanup deterministic. Docker Desktop and the built image must exist on the same Windows host as that process.

### Codex CLI

Register the server from PowerShell:

```powershell
codex mcp add code-sandbox -- `
  "D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe"

codex mcp list
```

Start a new Codex session and enter `/mcp` to confirm that `code-sandbox` is enabled. The Codex CLI and Codex IDE extension share MCP configuration on the same Codex host. See the official [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

For manual configuration, add this to `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.code-sandbox]
command = 'D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe'
startup_timeout_sec = 15
tool_timeout_sec = 150
```

Restart Codex after changing `config.toml`. To replace an old registration:

```powershell
codex mcp remove code-sandbox
codex mcp add code-sandbox -- `
  "D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe"
```

### Claude Code CLI

Register it as a user-scoped local stdio server:

```powershell
claude mcp add --transport stdio --scope user code-sandbox -- `
  "D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe"

claude mcp list
```

Start a new Claude Code session and enter `/mcp` to inspect its status. Claude requires all MCP options before the server name and uses `--` to separate the server command. See the official [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

To remove it:

```powershell
claude mcp remove code-sandbox --scope user
```

### Gemini CLI

Register it as a user-scoped stdio server:

```powershell
gemini mcp add --scope user --transport stdio code-sandbox `
  "D:\Tools\code-sandbox-mcp\.venv\Scripts\code-sandbox-mcp.exe"

gemini mcp list
```

Start a new Gemini CLI session and enter `/mcp list` to inspect its status. Keep Gemini's default confirmation behavior; do not add `--trust` for an execution server. See the official [Gemini CLI MCP documentation](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md).

To remove it:

```powershell
gemini mcp remove --scope user code-sandbox
```

### Other local stdio MCP clients

Clients that accept the common JSON MCP shape can use:

```json
{
  "mcpServers": {
    "code-sandbox": {
      "command": "D:\\Tools\\code-sandbox-mcp\\.venv\\Scripts\\code-sandbox-mcp.exe",
      "args": []
    }
  }
}
```

Configuration filenames and restart behavior are client-specific. Use the client's local **stdio** MCP option, not HTTP, SSE, or a Docker command. Do not configure secrets or host environment variables for this server.

### End-to-end smoke test

After registration, ask the client:

```text
Use the code-sandbox MCP tools to create a sandbox, write an index.js that
prints "Hello from the isolated container", run it, show stdout, and destroy
the sandbox even if a previous step fails.
```

While the request is running, a human can confirm that the managed container exists:

```powershell
docker ps --filter label=io.code-sandbox-mcp.managed=true
```

After `destroy_sandbox`, the command should show no container for that completed session.

## Tool schemas

All tool requests reject unexpected fields through strict Pydantic models. Paths are relative POSIX-style workspace paths after slash normalization. Absolute, drive, UNC, device, alternate-stream, null-byte, empty-component, dot-component, plain/encoded traversal, overlong, and over-deep paths are rejected. Before each operation the helper uses `lstat`; symlinks, hard links, and special files invalidate the workspace.

### `create_sandbox`

Input: `{}`

Output fields: `session_id`, `expires_at`, and fixed profile `javascript-offline`.

### `write_files`

```json
{
  "session_id": "opaque-id",
  "files": [{"path": "lib/parser.js", "content": "export const parse = value => value.trim();"}],
  "overwrite": false
}
```

Files are sent through Docker exec stdin to a fixed in-image writer, without a shell or host temporary file. The writer creates only validated relative paths with fixed modes. Contents are data, never command interpolation. The result contains a status (`written`, `overwritten`, or `exists`) for every submitted path. A failed transfer destroys the session rather than leaving a partially trusted workspace.

### `list_files`

Input fields: `session_id`, optional `path` (default `.`), `recursive` (default `true`), and `max_depth` from 0 through 10. Output entries contain relative `path`, `type`, and `size`, plus `truncated`.

### `read_file`

Input fields: `session_id`, `path`, and `max_bytes` (1 through 2 MiB; default 65536). Only regular UTF-8 text is returned. The result includes original `size` and `truncated`.

### `delete_files`

Input fields: `session_id` and one to 100 `paths`. Only regular files can be unlinked. The workspace root and directories cannot be deleted. Missing files are reported and do not make the operation fail.

### `run_javascript`

Input fields: `session_id`, a `.js`, `.mjs`, or `.cjs` `entrypoint`, up to 32 bounded `arguments`, and optional `timeout_seconds`. The host constructs this exact argument form without a shell:

```text
node --disable-proto=throw /workspace/<validated-entrypoint> <validated-arguments>
```

The result contains `exit_code`, UTF-8-decoded `stdout` and `stderr`, timeout and truncation flags, and `duration_ms`.

### `destroy_sandbox`

Input: `session_id`. The result says whether a live session was destroyed. Calling it again is safe.

Errors use stable codes including `INVALID_SESSION`, `SESSION_EXPIRED`, `INVALID_PATH`, `PATH_TRAVERSAL`, `FILE_TOO_LARGE`, `WORKSPACE_LIMIT_EXCEEDED`, `OUTPUT_LIMIT_EXCEEDED`, `TIMEOUT`, `CONTAINER_START_FAILED`, and `CONTAINER_REMOVAL_FAILED`. Responses never include stack traces, Docker daemon details, host paths, container IDs, or submitted content.

## Example Codex workflow

For "create three JavaScript files that normalize and deduplicate URLs, then run them," the client should:

1. Call `create_sandbox`.
2. Call `write_files` for `index.js`, `normalize.js`, and `test-data.js`.
3. Call `list_files` and verify all three paths.
4. Call `run_javascript` with `index.js`.
5. Inspect stdout and stderr, overwrite a file if necessary, and run again.
6. Call `destroy_sandbox` in all cases.

At no point do those files appear in the host repository or user profile. See `examples/codex_workflow.py` for the corresponding payload sequence.

## Audit log

Security audit logging is enabled by default. On Windows it is stored under `%LOCALAPPDATA%\code-sandbox-mcp\audit.jsonl`; on Linux it uses `$XDG_STATE_HOME` or `~/.local/state`. Set `CODE_SANDBOX_AUDIT_LOG` to choose another host location or `CODE_SANDBOX_AUDIT_ENABLED=false` to disable it.

Each JSONL record contains a timestamp, tool, SHA-256-derived session hash, result, duration, and relevant counts such as file/byte totals, exit code, timeout, output bytes, and cleanup result. It never logs source, raw session/container IDs, environment values, tokens, or Docker inspection data.

## Testing and manual verification

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m pyright src tests

docker build --pull -t code-sandbox-mcp-javascript:1.0.0 -f containers\Dockerfile.nodejs .
$env:RUN_DOCKER_TESTS='1'
& .\.venv\Scripts\python.exe -m pytest -q tests\test_docker_integration.py
```

While a human has a session open, inspect the managed container without giving its ID to the model:

```powershell
docker ps --filter label=io.code-sandbox-mcp.managed=true
docker inspect <container-id> --format '{{json .HostConfig}}'
```

Confirm `NetworkMode` is `none`, `ReadonlyRootfs` is true, `CapDrop` is `ALL`, no binds/devices/ports exist, and only `/workspace` and `/tmp` appear in `HostConfig.Tmpfs`. If Docker Desktop reports unsupported hardening settings or uses Windows containers, the sandbox should be treated as unavailable rather than weakened.

## Image and dependency updates

1. Select an exact Node patch tag and obtain its multi-platform digest from the official image registry.
2. Change both tag and digest in `containers/Dockerfile.nodejs` in a dedicated review.
3. Build the image, run unit and Docker integration tests, generate the SBOM, and run Trivy with high/critical failures enabled.
4. Retag only after review. Never add an MCP image parameter or automatic pull.
5. Update exact Python pins, regenerate `pylock.toml`, run `pip-audit`, and review Dependabot output.

See [MIGRATION.md](MIGRATION.md) for the intentionally breaking changes from the original project.

## Troubleshooting

- **`.venv\Scripts\code-sandbox-mcp.exe` is missing**: the project was not installed into that virtual environment. Run `& .\.venv\Scripts\python.exe -m pip install -e .`, then confirm the path with `Test-Path`.
- **The MCP client reports spawn, ENOENT, or file-not-found**: use the absolute executable path, confirm it with `Test-Path`, and restart the client after changing its configuration.
- **The MCP server appears but has no usable sandbox**: make sure Docker Desktop is running Linux containers and build `code-sandbox-mcp-javascript:1.0.0` locally. The server deliberately does not pull it.
- **`CONTAINER_UNAVAILABLE`**: start Docker Desktop in Linux-container mode and build the exact approved image tag locally.
- **Image profile-label error**: remove the incorrect local tag and rebuild from this Dockerfile.
- **Immediate session expiry**: check the validated `CODE_SANDBOX_*` server settings; the model cannot override them.
- **JavaScript cannot fetch/install**: expected; the default profile is permanently offline.
- **A session disappeared after an error**: transfer, execution, output, workspace-integrity, and host-watchdog failures deliberately fail closed and remove it.
