"""Illustrative MCP call sequence; use equivalent calls from your MCP client."""

WORKFLOW = [
    ("create_sandbox", {}),
    (
        "write_files",
        {
            "session_id": "<from-create-sandbox>",
            "files": [
                {"path": "index.js", "content": "import { normalize } from './normalize.js';"},
                {"path": "normalize.js", "content": "export const normalize = value => new URL(value).href;"},
                {"path": "test-data.js", "content": "export const urls = [];"},
            ],
            "overwrite": False,
        },
    ),
    ("list_files", {"session_id": "<from-create-sandbox>", "path": ".", "recursive": True}),
    ("run_javascript", {"session_id": "<from-create-sandbox>", "entrypoint": "index.js"}),
    ("destroy_sandbox", {"session_id": "<from-create-sandbox>"}),
]
