#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import hashlib
import shlex
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if re.search(r"(token|secret|password|passwd|api[_-]?key|authorization|credential)", str(key), re.I):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(x) for x in value[:50]]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", value)
        value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", "Bearer [REDACTED]", value, flags=re.I)
        return value[:4000]
    return value


def _safe_tool_input(value: Any) -> Any:
    if not isinstance(value, dict):
        return "[OMITTED]"
    out = {}
    for key, item in value.items():
        lk = str(key).lower()
        if lk in {"file_path", "path", "notebook_path"} and isinstance(item, str):
            out[key] = item[:1000]
        elif lk == "command" and isinstance(item, str):
            try:
                exe = shlex.split(item)[0] if shlex.split(item) else ""
            except Exception:
                exe = item.split(None,1)[0] if item.strip() else ""
            out[key] = {"executable": exe[:200], "sha256": hashlib.sha256(item.encode()).hexdigest(), "length": len(item)}
        else:
            raw = json.dumps(item, sort_keys=True, default=str)
            out[key] = {"sha256": hashlib.sha256(raw.encode()).hexdigest(), "length": len(raw)}
    return out




def _rotate(path: Path, max_bytes: int = 5 * 1024 * 1024, keep: int = 3) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        oldest = path.with_name(path.name + f".{keep}")
        oldest.unlink(missing_ok=True)
        for idx in range(keep - 1, 0, -1):
            src = path.with_name(path.name + f".{idx}")
            dst = path.with_name(path.name + f".{idx + 1}")
            if src.exists():
                src.replace(dst)
        if path.exists():
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass

def main() -> int:
    try:
        obj = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(obj, dict):
        return 0
    state_raw = os.environ.get("CLAUDE_AUTONOMY_STATE_DIR")
    if not state_raw:
        return 0
    state_dir = Path(state_raw).expanduser()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
    except OSError:
        pass

    event = str(obj.get("hook_event_name") or "unknown")
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "session_id": obj.get("session_id"),
        "permission_mode": obj.get("permission_mode"),
    }
    if event == "StopFailure":
        record.update({
            "error": obj.get("error"),
            "error_details": obj.get("error_details"),
            "last_assistant_message": {
                "sha256": hashlib.sha256(str(obj.get("last_assistant_message") or "").encode()).hexdigest(),
                "length": len(str(obj.get("last_assistant_message") or "")),
            },
        })
    elif event == "PermissionDenied":
        record.update({
            "tool_name": obj.get("tool_name"),
            "reason": obj.get("reason"),
            "tool_input": _safe_tool_input(obj.get("tool_input")),
            "mcp_server": obj.get("mcp_server"),
        })
    else:
        record["data"] = obj
    record = _redact(record)

    path = state_dir / "runtime-events.jsonl"
    _rotate(path)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        path.chmod(0o600)
    except OSError:
        pass
    # Telemetry only. Never override Claude Code's safety decision.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
