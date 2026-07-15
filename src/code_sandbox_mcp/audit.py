from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, enabled: bool, path: Path) -> None:
        self.enabled = enabled
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def session_hash(session_id: str | None) -> str | None:
        if session_id is None:
            return None
        return hashlib.sha256(session_id.encode("ascii")).hexdigest()[:16]

    def log(self, tool: str, session_id: str | None, result: str, duration_ms: int, **fields: Any) -> bool:
        if not self.enabled:
            return True
        allowed_fields = {
            "exit_code", "timed_out", "file_count", "submitted_bytes",
            "stdout_bytes", "stderr_bytes", "cleanup_result",
        }
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "session_hash": self.session_hash(session_id),
            "result": result,
            "duration_ms": max(0, duration_ms),
        }
        record.update({key: fields[key] for key in allowed_fields if key in fields})
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
        except OSError:
            return False
        return True
